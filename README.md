# Hands-on: a native program, an Anchor program, and wake.sol

Two small programs, each built for a different set of concepts, and Python tests that drive them
through wake.sol (ack3's test and fuzz harness for Solana, running litesvm in-process).

Checked on 2026-11-05 with solana-cli 4.1.2 (Agave), anchor-cli 1.2.2, Rust 1.99.1 (Anchor pins
1.89.0 through `rust-toolchain.toml`), wake.sol 0.1.0 and Python 3.13.

## Layout

| Path | What it is |
|:--|:--|
| `native-counter/` | A per-user counter with no framework: one entrypoint, hand-written checks |
| `anchor-vault/` | A time-locked SOL vault in Anchor: anyone deposits, only the owner withdraws after `unlock_at` |
| `pytypes/` | Python builders, account structs, events and errors generated from the vault's IDL |
| `tests/` | wake.sol tests for both programs, plus a stateful fuzz test for the vault |

## Setup

Linux or macOS. On Windows, install [WSL 2](https://learn.microsoft.com/windows/wsl/install) with Ubuntu
and run everything below inside it. You don't need Node.js, Yarn, a Solana wallet or a local
validator: the tests are Python and run the programs in-process.

1. **A C compiler, curl and git.** Rust needs a linker, and wake.sol is compiled from source.

   ```bash
   sudo apt install build-essential curl git    # Debian, Ubuntu, WSL
   xcode-select --install                       # macOS
   ```

2. **Rust**, plus the 1.89.0 toolchain that `anchor-vault/rust-toolchain.toml` pins:

   ```bash
   curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y
   . "$HOME/.cargo/env"
   rustup toolchain install 1.89.0
   ```

3. **Solana CLI 4.1.2** (Agave), which provides `cargo build-sbf`:

   ```bash
   sh -c "$(curl -sSfL https://release.anza.xyz/v4.1.2/install)"
   export PATH="$HOME/.local/share/solana/install/active_release/bin:$PATH"  # add this to your shell profile too
   ```

4. **Anchor CLI 1.2.2**, through Anchor's version manager:

   ```bash
   cargo install --git https://github.com/otter-sec/anchor avm --force
   avm install 1.2.2
   avm use 1.2.2
   ```

   The Anchor repository moved from `solana-foundation/anchor` to `otter-sec/anchor`, and the old URL
   redirects. Prebuilt `anchor` binaries are also on the
   [v1.2.2 release page](https://github.com/otter-sec/anchor/releases/tag/v1.2.2).

5. **wake.sol 0.1.0** in a virtualenv. It has no PyPI release yet, so this builds it from the commit
   these tests were checked against. uv downloads Python 3.13 if you don't have it.

   ```bash
   curl -LsSf https://astral.sh/uv/install.sh | sh
   uv venv ~/.venvs/wake-sol --python 3.13
   source ~/.venvs/wake-sol/bin/activate
   uv pip install "wake.sol @ git+https://github.com/ack3-ai/wake.sol@76e8931590c67199d2b50c52cf2076f1af29e2a7"
   ```

   The build took under a minute on a 32-core machine; expect a few minutes on a laptop.

Check the versions:

```bash
rustc --version          # 1.89 or newer
solana --version         # solana-cli 4.1.2
anchor --version         # anchor-cli 1.2.2
uv pip show wake.sol     # Version: 0.1.0
```

The first `cargo build-sbf` and the first `anchor build` each download Solana's platform tools
(v1.54 and v1.57, about 1.6 GB each unpacked), so run them before the session if the network is slow.

The one-line installer in [Anchor's docs](https://www.anchor-lang.com/docs/installation) sets up Rust,
the Solana CLI and Anchor in one go, but it takes the latest versions and adds Node.js and Yarn, so
what you get may differ from the versions checked here.

## Build and run

From this folder, with the virtualenv from setup step 5 activated:

```bash
(cd native-counter && cargo build-sbf)
(cd anchor-vault && anchor build --arch v0)                 # --arch v0: see "Known issues"
wake-sol gen --target-idl anchor-vault/target/idl --out pytypes  # after any change to the vault
wake-sol test                                               # 23 tests, well under a second
wake-sol test -s tests/test_native_counter.py               # -s shows the printed call traces
```

## The two programs

| | `native-counter` | `anchor-vault` |
|:--|:--|:--|
| State | PDA `["counter", user]`, 9 bytes: `count` u64, `bump` u8 | PDA `["vault", owner]`, 49 bytes: discriminator, `owner`, `unlock_at`, `bump` |
| Instructions | first data byte: `0` initialize, `1` add(amount u64) | `initialize(unlock_at)`, `deposit(amount)`, `withdraw(amount)` |
| Creating the PDA | `invoke_signed` into the System Program's `create_account` | the `init` constraint |
| Account checks | written by hand: owner, signer, type (length), address | `Account<VaultState>`, `Signer`, `has_one`, `seeds` + `bump` |
| Errors | built-in `ProgramError`s plus `Custom(0)` for overflow | `#[error_code]`: `StillLocked` (6000), `InsufficientFunds` (6001) |
| Moving SOL | none | deposit: CPI to the System Program; withdraw: direct debit, because the program owns the vault |

## What the tests show

`tests/test_native_counter.py`, raw instructions against a program with no IDL:

- **PDA + CPI**: `initialize` creates the counter through the System Program; the call trace shows the nested call
- **Atomic transactions**: two `add`s in one transaction; an overflow in the second reverts the first
- **Simulate**: `simulate` returns logs and compute units without writing state
- **The four checks**: one test each for signer, address (bob's counter), owner (a look-alike written with `svm.set_account`) and type (wrong length)
- **Instruction data**: malformed bytes are rejected
- **Custom errors**: a hand-written error class registered with `register_errors`
- **Dedup and blockhash**: resending identical bytes fails with `AlreadyProcessed`; a new blockhash makes a new transaction
- **Fees**: `svm.set_fees(lamports_per_signature=5_000)`; a sponsored transaction with two signers costs 10,000

`tests/test_anchor_vault.py`, builders generated from the IDL:

- **Typed state**: decode `VaultState`, including the 8-byte discriminator
- **Events**: `res.events == [Deposited(...)]`
- **Clock sysvar**: `StillLocked` until `svm.warp_to_timestamp(unlock_at)`
- **Rent**: the vault cannot be drained below its rent-exempt minimum
- **Generated checks**: `has_one` stops mallory; `Account<VaultState>` rejects a wallet (wrong owner) and an impostor account (wrong discriminator)

`tests/test_fuzz_vault.py`, stateful fuzzing: random deposits, withdrawals, thefts and clock jumps, with
invariants comparing the vault's balance and state to a Python model. Rerun any failure with
`wake-sol test --seed <hex> tests/test_fuzz_vault.py`.

## Exercises

1. Delete the owner check in `native-counter`'s `add` and rebuild. `test_owner_check` now fails with
   `ExternalAccountDataModified`: the runtime still blocks the write. When does the check matter?
   (Hint: accounts the program only reads.)
2. Delete the signer check in `add`. Which test fails, and with what error?
3. Delete `require!(amount <= available, ...)` in the vault's `withdraw` and rebuild. Both
   `test_the_vault_keeps_its_rent_exempt_minimum` and the fuzzer fail, with `InsufficientFundsForRent`
   or `ArithmeticOverflow`: again the runtime, not the program, is what stops it.
4. Add a `close` instruction to the vault (Anchor's `close = owner`) and a test that the rent deposit
   comes back to the owner.
5. Port the counter to Anchor and compare `res.compute_units_consumed` for the same `add`.

## Next steps: break things

Security challenges where you write the exploit yourself:

- **[Neodyme's Solana Security Workshop](https://workshop.neodyme.io/)**: start here. Five small native
  programs (levels 0–4), each with a bug to exploit, plus hints and solutions. The bugs are a missing
  owner check, a missing signer check, a lamport overflow, account type confusion and an arbitrary CPI:
  the checks from `native-counter`, seen from the attacker's side. It dates from 2021 and targets
  Solana 1.8 and Rust 1.56, which the setup above can't build, so use the Docker image or the pinned
  versions from its [setup page](https://workshop.neodyme.io/setup.html).
- **[neodyme-labs/solana-ctf](https://github.com/neodyme-labs/solana-ctf)**: harder challenges from
  past CTFs (ALLES!, Paradigm, justCTF, picoCTF and others), archived in 2022 with toolchains just as old.
- **[coral-xyz/sealevel-attacks](https://github.com/coral-xyz/sealevel-attacks)**: each common Solana
  bug as insecure code, a fix, and the idiomatic Anchor fix. For reading rather than exploiting.
- **[Solana's program security course](https://solana.com/developers/courses/program-security/security-intro)**:
  lessons built on sealevel-attacks.

## Known issues (wake.sol 0.1.0, 2026-11-05)

- **SBPF v3**: Anchor 1.2 builds SBPF v3 by default, which mainnet has run since epoch 993. wake.sol's
  bundled litesvm 0.12 / solana-sbpf 0.13.1 cannot load it: `add_program_from_file` raises
  `Instruction(InvalidAccountData)`, even with the v3 feature gate activated. Build with `--arch v0`,
  which is also `cargo build-sbf`'s default and still runs on mainnet.
- **Name collision**: `wake-sol gen` names the builder class after the program (`Vault`), which hides
  an account type of the same name. That is why the account is called `VaultState`.
- **`gen check`** does not accept `--target-idl`; use
  `wake-sol gen --target-idl anchor-vault/target/idl --out pytypes --check`.
- **Defaults that differ from mainnet**: no fees unless you call `svm.set_fees`; the older rent rate
  (890,880 lamports for 0 bytes, against 650,240 on mainnet); the clock starts at Unix time 0.
