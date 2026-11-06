//! A per-user counter, written without a framework.
//!
//! The first byte of the instruction data selects the instruction:
//!
//!   0  Initialize            accounts: [user (signer, writable), counter (writable), system program]
//!   1  Add { amount: u64 }   accounts: [user (signer), counter (writable)]
//!
//! Each user's counter lives at the PDA of ["counter", user] and is owned by this program.
//! Layout (9 bytes):
//!
//!   [0..8]  count   u64, little-endian
//!   [8]     bump    u8, the canonical bump, stored so later calls skip the search

use solana_program::{
    account_info::{next_account_info, AccountInfo},
    entrypoint,
    entrypoint::ProgramResult,
    msg,
    program::invoke_signed,
    program_error::ProgramError,
    pubkey::Pubkey,
    rent::Rent,
    sysvar::Sysvar,
};
use solana_system_interface::{instruction as system_instruction, program as system_program};

entrypoint!(process_instruction);

pub const COUNTER_SEED: &[u8] = b"counter";
pub const COUNTER_LEN: usize = 9;

/// Errors only this program raises, reported as `ProgramError::Custom(code)`.
pub enum CounterError {
    Overflow = 0,
}

impl From<CounterError> for ProgramError {
    fn from(e: CounterError) -> Self {
        ProgramError::Custom(e as u32)
    }
}

fn process_instruction(program_id: &Pubkey, accounts: &[AccountInfo], data: &[u8]) -> ProgramResult {
    // No ABI: we decide what the bytes mean.
    match data.split_first() {
        Some((0, [])) => initialize(program_id, accounts),
        Some((1, rest)) => {
            let amount: [u8; 8] = rest.try_into().map_err(|_| ProgramError::InvalidInstructionData)?;
            add(program_id, accounts, u64::from_le_bytes(amount))
        }
        _ => Err(ProgramError::InvalidInstructionData),
    }
}

fn initialize(program_id: &Pubkey, accounts: &[AccountInfo]) -> ProgramResult {
    let accounts = &mut accounts.iter();
    let user = next_account_info(accounts)?;
    let counter = next_account_info(accounts)?;
    let system = next_account_info(accounts)?;

    // signer check: the user pays for the counter, so they must sign
    if !user.is_signer {
        return Err(ProgramError::MissingRequiredSignature);
    }
    // address check: the counter must be this user's PDA
    let (expected, bump) = Pubkey::find_program_address(&[COUNTER_SEED, user.key.as_ref()], program_id);
    if counter.key != &expected {
        return Err(ProgramError::InvalidSeeds);
    }
    // program check: call the real System Program, not a look-alike
    if system.key != &system_program::ID {
        return Err(ProgramError::IncorrectProgramId);
    }

    // CPI: the System Program funds and allocates the account, then makes us its owner.
    // The PDA has no private key; it "signs" because we pass the seeds that derive it.
    let lamports = Rent::get()?.minimum_balance(COUNTER_LEN);
    invoke_signed(
        &system_instruction::create_account(user.key, counter.key, lamports, COUNTER_LEN as u64, program_id),
        &[user.clone(), counter.clone(), system.clone()],
        &[&[COUNTER_SEED, user.key.as_ref(), &[bump]]],
    )?;

    // New account data starts zeroed, so count = 0; only the bump needs writing.
    counter.try_borrow_mut_data()?[8] = bump;
    msg!("counter created for {}", user.key);
    Ok(())
}

fn add(program_id: &Pubkey, accounts: &[AccountInfo], amount: u64) -> ProgramResult {
    let accounts = &mut accounts.iter();
    let user = next_account_info(accounts)?;
    let counter = next_account_info(accounts)?;

    // owner check: only accounts owned by this program hold real counters
    if counter.owner != program_id {
        return Err(ProgramError::IncorrectProgramId);
    }
    // signer check: only the user may change their counter
    if !user.is_signer {
        return Err(ProgramError::MissingRequiredSignature);
    }
    // type check: a counter is exactly 9 bytes
    let mut data = counter.try_borrow_mut_data()?;
    if data.len() != COUNTER_LEN {
        return Err(ProgramError::InvalidAccountData);
    }
    // address check: it is this user's PDA, not another user's counter
    let expected = Pubkey::create_program_address(&[COUNTER_SEED, user.key.as_ref(), &[data[8]]], program_id)
        .map_err(|_| ProgramError::InvalidSeeds)?;
    if counter.key != &expected {
        return Err(ProgramError::InvalidSeeds);
    }

    let mut count = [0u8; 8];
    count.copy_from_slice(&data[..8]);
    let count = u64::from_le_bytes(count).checked_add(amount).ok_or(CounterError::Overflow)?;
    data[..8].copy_from_slice(&count.to_le_bytes());
    msg!("count = {}", count);
    Ok(())
}
