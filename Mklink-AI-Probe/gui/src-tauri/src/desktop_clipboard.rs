#[cfg(target_os = "windows")]
struct ClipboardGuard;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct ClipboardOwner(isize);

impl ClipboardOwner {
    pub fn from_raw(raw: isize) -> Result<Self, String> {
        if raw == 0 {
            Err("A valid window handle is required to own the Windows clipboard".into())
        } else {
            Ok(Self(raw))
        }
    }

    #[cfg(target_os = "windows")]
    fn as_hwnd(self) -> windows_sys::Win32::Foundation::HWND {
        self.0 as windows_sys::Win32::Foundation::HWND
    }
}

#[cfg(target_os = "windows")]
impl ClipboardGuard {
    fn open(owner: Option<ClipboardOwner>) -> Result<Self, String> {
        use windows_sys::Win32::System::DataExchange::OpenClipboard;

        let hwnd = owner
            .map(ClipboardOwner::as_hwnd)
            .unwrap_or(std::ptr::null_mut());

        for attempt in 0..10 {
            if unsafe { OpenClipboard(hwnd) } != 0 {
                return Ok(Self);
            }
            if attempt < 9 {
                std::thread::sleep(std::time::Duration::from_millis(5));
            }
        }
        Err("Unable to open the Windows clipboard".into())
    }
}

#[cfg(target_os = "windows")]
impl Drop for ClipboardGuard {
    fn drop(&mut self) {
        unsafe {
            windows_sys::Win32::System::DataExchange::CloseClipboard();
        }
    }
}

#[cfg(target_os = "windows")]
struct GlobalUnlockGuard(*mut std::ffi::c_void);

#[cfg(target_os = "windows")]
impl Drop for GlobalUnlockGuard {
    fn drop(&mut self) {
        unsafe {
            windows_sys::Win32::System::Memory::GlobalUnlock(self.0);
        }
    }
}

#[cfg(target_os = "windows")]
struct GlobalMemory(*mut std::ffi::c_void);

#[cfg(target_os = "windows")]
impl Drop for GlobalMemory {
    fn drop(&mut self) {
        unsafe {
            windows_sys::Win32::Foundation::GlobalFree(self.0);
        }
    }
}

#[cfg(target_os = "windows")]
pub fn read_text() -> Result<String, String> {
    use std::slice;
    use windows_sys::Win32::System::DataExchange::{GetClipboardData, IsClipboardFormatAvailable};
    use windows_sys::Win32::System::Memory::{GlobalLock, GlobalSize};

    const CF_UNICODETEXT: u32 = 13;
    // Reading does not claim clipboard ownership, so a window handle is not
    // required for this operation.
    let _clipboard = ClipboardGuard::open(None)?;
    unsafe {
        if IsClipboardFormatAvailable(CF_UNICODETEXT) == 0 {
            return Ok(String::new());
        }
        let handle = GetClipboardData(CF_UNICODETEXT);
        if handle.is_null() {
            return Err("Unable to read text from the Windows clipboard".into());
        }
        let size = GlobalSize(handle);
        if size < std::mem::size_of::<u16>() {
            return Err("The Windows clipboard contains invalid text".into());
        }
        let source = GlobalLock(handle) as *const u16;
        if source.is_null() {
            return Err("Unable to lock the Windows clipboard".into());
        }
        let _locked = GlobalUnlockGuard(handle);
        let units = slice::from_raw_parts(source, size / std::mem::size_of::<u16>());
        let length = units
            .iter()
            .position(|unit| *unit == 0)
            .unwrap_or(units.len());
        String::from_utf16(&units[..length])
            .map_err(|_| "The Windows clipboard contains invalid text".to_string())
    }
}

#[cfg(target_os = "windows")]
pub fn write_text(owner: ClipboardOwner, text: &str) -> Result<(), String> {
    use std::ptr::copy_nonoverlapping;
    use windows_sys::Win32::System::DataExchange::{EmptyClipboard, SetClipboardData};
    use windows_sys::Win32::System::Memory::{GlobalAlloc, GlobalLock, GMEM_MOVEABLE};

    const CF_UNICODETEXT: u32 = 13;
    let mut wide: Vec<u16> = text.encode_utf16().collect();
    wide.push(0);
    unsafe {
        let memory = GlobalMemory(GlobalAlloc(
            GMEM_MOVEABLE,
            wide.len() * std::mem::size_of::<u16>(),
        ));
        if memory.0.is_null() {
            return Err("Unable to allocate clipboard memory".into());
        }
        let target = GlobalLock(memory.0) as *mut u16;
        if target.is_null() {
            return Err("Unable to lock clipboard memory".into());
        }
        {
            let _locked = GlobalUnlockGuard(memory.0);
            copy_nonoverlapping(wide.as_ptr(), target, wide.len());
        }

        // EmptyClipboard assigns ownership to the HWND passed to
        // OpenClipboard. Passing NULL here makes the owner NULL and causes
        // SetClipboardData to fail, so writes must always use the invoking
        // Tauri window's HWND.
        let _clipboard = ClipboardGuard::open(Some(owner))?;
        if EmptyClipboard() == 0 {
            return Err("Unable to clear the Windows clipboard".into());
        }
        if SetClipboardData(CF_UNICODETEXT, memory.0).is_null() {
            return Err("Unable to write the Windows clipboard".into());
        }
        // SetClipboardData transfers ownership to the system on success.
        std::mem::forget(memory);
    }
    Ok(())
}

#[cfg(not(target_os = "windows"))]
pub fn read_text() -> Result<String, String> {
    Err("Desktop clipboard access is available only on Windows".into())
}

#[cfg(not(target_os = "windows"))]
pub fn write_text(_owner: ClipboardOwner, _text: &str) -> Result<(), String> {
    Err("Desktop clipboard access is available only on Windows".into())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn clipboard_owner_rejects_a_null_window_handle() {
        assert!(ClipboardOwner::from_raw(0).is_err());
    }

    #[test]
    fn clipboard_owner_preserves_a_nonzero_window_handle() {
        assert_eq!(
            ClipboardOwner::from_raw(0x1234).unwrap(),
            ClipboardOwner(0x1234)
        );
    }
}
