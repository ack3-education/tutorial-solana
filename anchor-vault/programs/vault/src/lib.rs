//! A time-locked SOL vault, written with Anchor.
//!
//! Each owner has one vault at the PDA of ["vault", owner]. Anyone can deposit SOL into it;
//! only the owner can withdraw, and only once `unlock_at` (a Unix timestamp) has passed.
//! The vault account holds both its state and the deposited lamports.
//!
//! Compare with ../../native-counter: every check written by hand there is an account type
//! or an `#[account(...)]` constraint here.

use anchor_lang::prelude::*;
use anchor_lang::system_program::{transfer, Transfer};

declare_id!("6EEouQ3YSUdxiKZUjLSV2Pfaz9LErX154Mf2aBzxtNvv");

#[program]
pub mod vault {
    use super::*;

    pub fn initialize(ctx: Context<Initialize>, unlock_at: i64) -> Result<()> {
        let vault = &mut ctx.accounts.vault;
        vault.owner = ctx.accounts.owner.key();
        vault.unlock_at = unlock_at;
        vault.bump = ctx.bumps.vault;
        Ok(())
    }

    pub fn deposit(ctx: Context<Deposit>, amount: u64) -> Result<()> {
        // CPI into the System Program: the depositor signs, lamports move into the vault.
        transfer(
            CpiContext::new(
                anchor_lang::system_program::ID,
                Transfer {
                    from: ctx.accounts.depositor.to_account_info(),
                    to: ctx.accounts.vault.to_account_info(),
                },
            ),
            amount,
        )?;
        emit!(Deposited {
            vault: ctx.accounts.vault.key(),
            depositor: ctx.accounts.depositor.key(),
            amount,
        });
        Ok(())
    }

    pub fn withdraw(ctx: Context<Withdraw>, amount: u64) -> Result<()> {
        let now = Clock::get()?.unix_timestamp;
        require!(now >= ctx.accounts.vault.unlock_at, VaultError::StillLocked);

        let vault = ctx.accounts.vault.to_account_info();
        let rent_minimum = Rent::get()?.minimum_balance(vault.data_len());
        let available = vault.lamports().saturating_sub(rent_minimum);
        require!(amount <= available, VaultError::InsufficientFunds);

        // No CPI here: this program owns the vault, and an owner may debit its lamports directly.
        vault.sub_lamports(amount)?;
        ctx.accounts.owner.add_lamports(amount)?;
        emit!(Withdrawn {
            vault: vault.key(),
            amount,
        });
        Ok(())
    }
}

#[derive(Accounts)]
pub struct Initialize<'info> {
    #[account(mut)]
    pub owner: Signer<'info>,
    // init: CPI to the System Program to create the account, owned by this program,
    // at the PDA of these seeds; Anchor finds the canonical bump and writes the discriminator.
    #[account(
        init,
        payer = owner,
        space = 8 + VaultState::INIT_SPACE,
        seeds = [b"vault", owner.key().as_ref()],
        bump,
    )]
    pub vault: Account<'info, VaultState>,
    pub system_program: Program<'info, System>,
}

#[derive(Accounts)]
pub struct Deposit<'info> {
    #[account(mut)]
    pub depositor: Signer<'info>,
    // Account<VaultState> checks the owner (this program) and the 8-byte discriminator;
    // seeds + bump check the address.
    #[account(mut, seeds = [b"vault", vault.owner.as_ref()], bump = vault.bump)]
    pub vault: Account<'info, VaultState>,
    pub system_program: Program<'info, System>,
}

#[derive(Accounts)]
pub struct Withdraw<'info> {
    #[account(mut)]
    pub owner: Signer<'info>,
    // has_one = owner: vault.owner must equal the `owner` account, which must have signed.
    #[account(mut, has_one = owner, seeds = [b"vault", vault.owner.as_ref()], bump = vault.bump)]
    pub vault: Account<'info, VaultState>,
}

// Named VaultState rather than Vault: `wake-sol gen` names its builder class after the
// program (`Vault`), which would hide an account type of the same name.
#[account]
#[derive(InitSpace)]
pub struct VaultState {
    pub owner: Pubkey,
    pub unlock_at: i64,
    pub bump: u8,
}

#[event]
pub struct Deposited {
    pub vault: Pubkey,
    pub depositor: Pubkey,
    pub amount: u64,
}

#[event]
pub struct Withdrawn {
    pub vault: Pubkey,
    pub amount: u64,
}

#[error_code]
pub enum VaultError {
    #[msg("The vault is still locked")]
    StillLocked,
    #[msg("Withdrawal would leave the vault below its rent-exempt minimum")]
    InsufficientFunds,
}
