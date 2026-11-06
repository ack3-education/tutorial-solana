"""Shared setup: where the built programs are, and how to load them into the test SVM."""

from pathlib import Path

import pytest

from wake_sol import Account, Pubkey, sol, svm

ROOT = Path(__file__).resolve().parent.parent

COUNTER_SO = ROOT / "native-counter/target/deploy/native_counter.so"
VAULT_SO = ROOT / "anchor-vault/target/deploy/vault.so"

# A native program can be loaded at any address, so pick a readable one.
# The Anchor program must sit at its declare_id!, which `wake-sol gen` exports as PROGRAM_ID.
COUNTER_ID = Pubkey("Counter111111111111111111111111111111111111")


def load_program(program_id: Pubkey, so: Path, build_hint: str) -> None:
    """Deploy a built .so into the (freshly reset) global SVM, or skip the test if it isn't built."""
    if not so.exists():
        pytest.skip(f"{so.name} is not built; run `{build_hint}`")
    svm.add_program_from_file(program_id, so)  # cheatcode: no loader, no upgrade authority
    Account(program_id).label = so.stem


def load_counter() -> None:
    load_program(COUNTER_ID, COUNTER_SO, "cd native-counter && cargo build-sbf")


def load_vault() -> None:
    from pytypes.vault import PROGRAM_ID

    # --arch v0: wake.sol 0.1.0 cannot load Anchor's default SBPF v3 output yet (see README)
    load_program(PROGRAM_ID, VAULT_SO, "cd anchor-vault && anchor build --arch v0")


def new_user(label: str, lamports: int = sol(10)) -> Account:
    """A funded keypair account with a name that shows up in call traces."""
    user = Account.new()
    user.label = label
    svm.airdrop(user, lamports)
    return user
