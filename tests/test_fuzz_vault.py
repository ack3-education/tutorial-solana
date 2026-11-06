"""Stateful fuzzing of the Anchor vault with wake.sol.

The engine picks flows at random (by weight) and checks every invariant after each one.
A Python model (`self.balance`) predicts what the vault should hold; the invariants
compare it with the chain. Any failure reproduces from the printed seed:
    wake-sol test --seed <hex> tests/test_fuzz_vault.py
"""

from helpers import load_vault, new_user
from pytypes.vault import PROGRAM_ID, Vault, VaultState
from wake_sol import AnchorError, Account, FuzzTest, flow, invariant, must_fail, random, sol, svm

LOCK_SECONDS = 3_600


def vault_of(owner: Account) -> Account:
    pda, _bump = Account.find_program_address([b"vault", owner], PROGRAM_ID)
    return pda


class VaultFuzz(FuzzTest):
    def pre_sequence(self):
        # Every sequence starts from an empty chain, so rebuild the world here.
        load_vault()
        self.owner = new_user("owner", sol(100))
        self.depositors = [new_user(f"depositor{i}", sol(100)) for i in range(3)]
        self.vault = vault_of(self.owner)
        self.unlock_at = svm.clock.unix_timestamp + LOCK_SECONDS
        self.owner.tx(Vault.initialize(self.unlock_at, owner=self.owner, vault=self.vault))
        self.rent = svm.minimum_balance_for_rent_exemption(len(self.vault.data))
        self.balance = 0  # model: lamports deposited and not yet withdrawn

    @flow(weight=300)
    def deposit(self):
        who = random.choice(self.depositors)
        amount = random.randint(1, sol(1))
        who.tx(Vault.deposit(amount, depositor=who, vault=self.vault))
        self.balance += amount

    @flow(weight=200)
    def withdraw(self):
        if random.randint(1, 4) == 1:  # a quarter of the time, ask for more than is there
            amount = self.balance + random.randint(1, sol(1))
        else:
            amount = random.randint(0, self.balance)
        ix = Vault.withdraw(amount, owner=self.owner, vault=self.vault)
        if svm.clock.unix_timestamp < self.unlock_at:
            with must_fail(Vault.StillLocked):
                self.owner.tx(ix)
        elif amount > self.balance:
            with must_fail(Vault.InsufficientFunds):
                self.owner.tx(ix)
        else:
            self.owner.tx(ix)
            self.balance -= amount

    @flow(weight=100)
    def withdraw_as_someone_else(self):
        thief = random.choice(self.depositors)
        # account constraints run before the handler, so has_one fails even while locked
        with must_fail(AnchorError.ConstraintHasOne):
            thief.tx(Vault.withdraw(1, owner=thief, vault=self.vault))

    @flow(weight=50)
    def let_time_pass(self):
        svm.warp_to_timestamp(svm.clock.unix_timestamp + random.randint(60, LOCK_SECONDS))

    @invariant()
    def vault_holds_rent_plus_deposits(self):
        assert self.vault.lamports == self.rent + self.balance

    @invariant(period=5)
    def state_is_unchanged(self):
        state = VaultState.decode(self.vault.data)
        assert state.owner == self.owner.pubkey and state.unlock_at == self.unlock_at


def test_fuzz_vault():
    VaultFuzz.run(sequences_count=10, flows_count=50)
