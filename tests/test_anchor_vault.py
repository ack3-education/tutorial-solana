"""The Anchor vault, driven from Python with builders generated from its IDL.

Regenerate the builders after changing the program:
    wake-sol gen --target-idl anchor-vault/target/idl --out pytypes

Concepts: typed builders and account structs, events, custom errors, Anchor's generated
account checks, the Clock sysvar (and warping it), and lamports owned by a program.
"""

import pytest

from helpers import load_vault, new_user
from pytypes.vault import PROGRAM_ID, Deposited, Vault, VaultState, Withdrawn
from wake_sol import AnchorError, Account, must_fail, print, sol, svm

LOCK_SECONDS = 3_600


@pytest.fixture(autouse=True)
def program():
    load_vault()


def vault_of(owner: Account) -> Account:
    pda, _bump = Account.find_program_address([b"vault", owner], PROGRAM_ID)
    return pda


def open_vault(owner: Account) -> tuple[Account, int]:
    vault = vault_of(owner)
    unlock_at = svm.clock.unix_timestamp + LOCK_SECONDS
    owner.tx(Vault.initialize(unlock_at, owner=owner, vault=vault))
    return vault, unlock_at


# --- happy path ----------------------------------------------------------------------------


def test_initialize_writes_typed_state():
    alice = new_user("alice")
    vault = vault_of(alice)
    unlock_at = svm.clock.unix_timestamp + LOCK_SECONDS

    res = alice.tx(Vault.initialize(unlock_at, owner=alice, vault=vault))
    print(res.call_trace)  # Anchor's `init` is a CPI into the System Program

    _, bump = Account.find_program_address([b"vault", alice], PROGRAM_ID)
    assert vault.owner == PROGRAM_ID
    assert vault.data[:8] == VaultState.__borsh_meta__.discriminator  # Anchor's type tag
    assert VaultState.decode(vault.data) == VaultState(owner=alice.pubkey, unlock_at=unlock_at, bump=bump)


def test_anyone_can_deposit_and_events_are_decoded():
    alice, bob = new_user("alice"), new_user("bob")
    vault, _ = open_vault(alice)
    before = vault.lamports

    res = bob.tx(Vault.deposit(sol(1), depositor=bob, vault=vault))
    print(res.call_trace)  # shows the System Program transfer and a ⚡ Deposited event

    assert res.events == [Deposited(vault=vault.pubkey, depositor=bob.pubkey, amount=sol(1))]
    assert vault.lamports == before + sol(1)


def test_withdraw_waits_for_the_unlock_time():
    alice = new_user("alice")
    vault, unlock_at = open_vault(alice)
    alice.tx(Vault.deposit(sol(2), depositor=alice, vault=vault))

    with must_fail(Vault.StillLocked):
        alice.tx(Vault.withdraw(sol(1), owner=alice, vault=vault))

    svm.warp_to_timestamp(unlock_at)  # cheatcode: move the Clock sysvar forward
    svm.expire_blockhash()  # the failed attempt landed too; identical bytes would be a duplicate
    before = alice.lamports
    res = alice.tx(Vault.withdraw(sol(1), owner=alice, vault=vault))

    assert res.events == [Withdrawn(vault=vault.pubkey, amount=sol(1))]
    assert alice.lamports == before + sol(1)  # wake.sol charges no fees unless you set them


def test_the_vault_keeps_its_rent_exempt_minimum():
    alice = new_user("alice")
    vault, unlock_at = open_vault(alice)
    alice.tx(Vault.deposit(sol(1), depositor=alice, vault=vault))
    svm.warp_to_timestamp(unlock_at)

    with must_fail(Vault.InsufficientFunds):
        alice.tx(Vault.withdraw(sol(1) + 1, owner=alice, vault=vault))

    alice.tx(Vault.withdraw(sol(1), owner=alice, vault=vault))
    assert vault.lamports == svm.minimum_balance_for_rent_exemption(len(vault.data))


# --- the checks Anchor generated -------------------------------------------------------------


def test_only_the_owner_can_withdraw():
    alice, mallory = new_user("alice"), new_user("mallory")
    vault, unlock_at = open_vault(alice)
    alice.tx(Vault.deposit(sol(1), depositor=alice, vault=vault))
    svm.warp_to_timestamp(unlock_at)

    # mallory signs as `owner`, but the vault stores alice: `has_one = owner` rejects it
    with must_fail(AnchorError.ConstraintHasOne):
        mallory.tx(Vault.withdraw(sol(1), owner=mallory, vault=vault))


def test_account_type_rejects_a_wallet():
    alice, bob = new_user("alice"), new_user("bob")

    # bob's wallet is owned by the System Program, not by the vault program
    with must_fail(AnchorError.AccountOwnedByWrongProgram):
        alice.tx(Vault.deposit(sol(1), depositor=alice, vault=bob))


def test_account_type_rejects_another_struct():
    alice = new_user("alice")
    # Cheatcode: an account owned by the vault program, the right size, but without
    # VaultState's 8-byte discriminator, as if it were some other account type.
    impostor = Account.new()
    size = len(VaultState(owner=alice.pubkey, unlock_at=0, bump=0).encode())
    svm.set_account(impostor, lamports=svm.minimum_balance_for_rent_exemption(size),
                    data=bytes(size), owner=PROGRAM_ID)

    with must_fail(AnchorError.AccountDiscriminatorMismatch):
        alice.tx(Vault.deposit(sol(1), depositor=alice, vault=impostor))
