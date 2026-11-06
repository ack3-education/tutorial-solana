"""The native counter, driven from Python with raw instructions.

Concepts: PDAs, a CPI into the System Program, instruction data, atomic transactions,
each of the four account checks, signature dedup, and base fees.
"""

from dataclasses import dataclass

import pytest

from helpers import COUNTER_ID, load_counter, new_user
from wake_sol import (
    SYSTEM_PROGRAM_ID,
    Account,
    BorshStruct,
    Instruction,
    ProgramError,
    SolanaError,
    SystemProgramError,
    must_fail,
    print,
    readonly,
    register_errors,
    signer,
    svm,
    u8,
    u64,
    writable,
    writable_signer,
)


@pytest.fixture(autouse=True)
def program():
    load_counter()  # the SVM is reset before every test, so load the program each time


# --- the program's interface, written by hand: a native program has no IDL ----------------


@dataclass
class Counter(BorshStruct):  # the 9-byte account layout: count, then bump
    count: u64
    bump: u8


class CounterError(ProgramError):  # ProgramError::Custom codes from src/lib.rs
    pass


class Overflow(CounterError):
    code = 0
    msg = "count would overflow u64"


register_errors(CounterError, COUNTER_ID)


def counter_of(user: Account) -> Account:
    pda, _bump = Account.find_program_address([b"counter", user], COUNTER_ID)
    return pda


def initialize(user: Account) -> Instruction:
    accounts = [writable_signer(user), writable(counter_of(user)), SYSTEM_PROGRAM_ID]
    return Instruction(COUNTER_ID, accounts, bytes([0]))


def add(user: Account, amount: int, counter: Account | None = None) -> Instruction:
    accounts = [signer(user), writable(counter or counter_of(user))]
    return Instruction(COUNTER_ID, accounts, bytes([1]) + amount.to_bytes(8, "little"))


# --- happy path ----------------------------------------------------------------------------


def test_initialize_creates_a_pda_owned_by_the_program():
    alice = new_user("alice")
    counter, bump = Account.find_program_address([b"counter", alice], COUNTER_ID)

    res = alice.tx(initialize(alice))
    print(res.call_trace)  # the System Program CPI appears as a child of our instruction

    assert res.call_trace[0].inner[0].program_id == SYSTEM_PROGRAM_ID
    assert counter.owner == COUNTER_ID  # the System Program created it, then assigned it to us
    assert counter.lamports == svm.minimum_balance_for_rent_exemption(9)
    assert Counter.decode(counter.data) == Counter(count=0, bump=bump)


def test_instructions_in_one_transaction_run_in_order():
    alice = new_user("alice")
    alice.tx(initialize(alice))

    alice.tx(add(alice, 30), add(alice, 12))  # two instructions, one transaction
    assert Counter.decode(counter_of(alice).data).count == 42


def test_simulate_runs_without_committing():
    alice = new_user("alice")
    alice.tx(initialize(alice), add(alice, 1))

    sim = alice.simulate(add(alice, 100))
    print(sim.logs)  # ... 'Program log: count = 101' ...
    assert Counter.decode(counter_of(alice).data).count == 1  # nothing was written


# --- failures ------------------------------------------------------------------------------


def test_a_failing_instruction_reverts_the_whole_transaction():
    alice = new_user("alice")
    alice.tx(initialize(alice))

    with must_fail(Overflow) as e:  # resolved to our class through register_errors
        alice.tx(add(alice, 1), add(alice, u64.max))
    assert e.value.instruction_index == 1
    assert Counter.decode(counter_of(alice).data).count == 0  # the first add was rolled back too


def test_initializing_twice_fails_inside_the_cpi():
    alice = new_user("alice")
    alice.tx(initialize(alice))
    svm.expire_blockhash()  # otherwise the identical transaction is rejected as a duplicate

    with must_fail(SystemProgramError.AccountAlreadyInUse) as e:
        alice.tx(initialize(alice))
    print(e.value.call_trace)  # the error comes from the System Program, one level down


def test_signer_check():
    alice, mallory = new_user("alice"), new_user("mallory")
    alice.tx(initialize(alice))

    # mallory pays and lists alice's key, but alice never signs
    forged = Instruction(COUNTER_ID, [readonly(alice), writable(counter_of(alice))], add(alice, 1).data)
    with must_fail(SolanaError.MissingRequiredSignature):
        mallory.tx(forged)


def test_address_check():
    alice, bob = new_user("alice"), new_user("bob")
    alice.tx(initialize(alice))
    bob.tx(initialize(bob))

    # a real counter, owned by the program, but it belongs to bob
    with must_fail(SolanaError.InvalidSeeds):
        alice.tx(add(alice, 1, counter=counter_of(bob)))


def test_owner_check():
    alice = new_user("alice")
    counter, bump = Account.find_program_address([b"counter", alice], COUNTER_ID)
    # Cheatcode: write any state we like. Here, a look-alike counter at the right
    # address with the right layout and bump, but owned by the System Program instead of ours.
    fake = Counter(count=1_000_000, bump=bump).encode()
    svm.set_account(counter, lamports=svm.minimum_balance_for_rent_exemption(9), data=fake,
                    owner=SYSTEM_PROGRAM_ID)

    # Without the owner check, the runtime would still refuse the write
    # (ExternalAccountDataModified): only the owner may change data. The check is what
    # protects accounts a program only reads, such as a config or a price account.
    with must_fail(SolanaError.IncorrectProgramId):
        alice.tx(add(alice, 1))


def test_type_check():
    alice = new_user("alice")
    # owned by our program, at the right address, but 8 bytes instead of 9
    svm.set_account(counter_of(alice), lamports=svm.minimum_balance_for_rent_exemption(8),
                    data=bytes(8), owner=COUNTER_ID)

    with must_fail(SolanaError.InvalidAccountData):
        alice.tx(add(alice, 1))


@pytest.mark.parametrize("data", [b"", b"\x07", b"\x01\x05", b"\x00\x00"])
def test_malformed_instruction_data(data):
    alice = new_user("alice")
    with must_fail(SolanaError.InvalidInstructionData):
        alice.tx(Instruction(COUNTER_ID, [signer(alice), writable(counter_of(alice))], data))


# --- transactions and fees -------------------------------------------------------------------


def test_resending_the_same_transaction_is_deduplicated():
    alice = new_user("alice")
    alice.tx(initialize(alice))
    alice.tx(add(alice, 1))

    with must_fail(SolanaError.AlreadyProcessed):  # same bytes, same signature: runs once
        alice.tx(add(alice, 1))
    assert Counter.decode(counter_of(alice).data).count == 1

    svm.expire_blockhash()  # a new blockhash makes it a different transaction
    alice.tx(add(alice, 1))
    assert Counter.decode(counter_of(alice).data).count == 2


def test_base_fee_is_charged_per_signature():
    svm.set_fees(lamports_per_signature=5_000)  # wake.sol charges no fees unless asked
    alice, sponsor = new_user("alice"), new_user("sponsor")
    alice.tx(initialize(alice))

    before = sponsor.lamports
    sponsor.tx(add(alice, 1))  # sponsor pays; alice still signs, so the transaction has 2 signatures
    assert before - sponsor.lamports == 2 * 5_000
