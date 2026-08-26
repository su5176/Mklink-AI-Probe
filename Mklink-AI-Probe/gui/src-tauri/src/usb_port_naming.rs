use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;
use std::mem::size_of;
use std::path::{Path, PathBuf};
use windows::core::{GUID, PCWSTR};
use windows::Win32::Devices::DeviceAndDriverInstallation::{
    CM_Reenumerate_DevNode, CM_Set_DevNode_PropertyW, SetupDiDestroyDeviceInfoList,
    SetupDiEnumDeviceInfo, SetupDiGetClassDevsW, SetupDiGetDeviceInstanceIdW,
    SetupDiGetDevicePropertyW, CM_REENUMERATE_SYNCHRONOUS, CR_SUCCESS, HDEVINFO,
    SP_DEVINFO_DATA, DIGCF_ALLCLASSES, DIGCF_PRESENT,
};
use windows::Win32::Devices::Properties::{
    DEVPKEY_Device_BusReportedDeviceDesc, DEVPKEY_Device_ContainerId, DEVPKEY_Device_DeviceDesc,
    DEVPKEY_Device_Parent, DEVPKEY_Device_FriendlyName, DEVPROPTYPE, DEVPROP_TYPE_GUID,
    DEVPROP_TYPE_STRING,
};
use winreg::enums::{HKEY_LOCAL_MACHINE, KEY_READ, KEY_WRITE};
use winreg::RegKey;

const USB_PREFIX: &str = r"USB\VID_0D28&PID_0202";

#[derive(Debug, Clone, PartialEq, Eq)]
struct DeviceRecord {
    instance_id: String,
    bus_description: String,
    device_description: String,
    container_id: GUID,
    parent: String,
    dev_inst: u32,
}

#[derive(Debug, Clone, PartialEq, Eq)]
struct PortCandidate {
    instance_id: String,
    bus_description: String,
    device_description: String,
    container_id: GUID,
    parent: String,
    mi: String,
    port_name: String,
    target_name: String,
    current_root_name: Option<String>,
    current_parameters_name: Option<String>,
    registry_path: String,
    dev_inst: u32,
}

#[derive(Debug, Default, Deserialize, Serialize)]
struct BackupFile {
    entries: BTreeMap<String, FriendlyNameBackup>,
}

#[derive(Debug, Clone, Deserialize, Serialize)]
struct FriendlyNameBackup {
    root: Option<String>,
    parameters: Option<String>,
}

#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct UsbNamingResult {
    pub status: String,
    pub matched_devices: usize,
    pub ports: usize,
    pub changed: usize,
}

struct DeviceInfoSet(HDEVINFO);

impl Drop for DeviceInfoSet {
    fn drop(&mut self) {
        unsafe {
            let _ = SetupDiDestroyDeviceInfoList(self.0);
        }
    }
}

fn wide_string(bytes: &[u8]) -> String {
    let words = bytes
        .chunks_exact(2)
        .map(|pair| u16::from_le_bytes([pair[0], pair[1]]))
        .take_while(|word| *word != 0)
        .collect::<Vec<_>>();
    String::from_utf16_lossy(&words)
}

unsafe fn device_property(
    set: HDEVINFO,
    data: &SP_DEVINFO_DATA,
    key: *const windows::Win32::Foundation::DEVPROPKEY,
) -> Result<(DEVPROPTYPE, Vec<u8>), String> {
    let mut property_type = DEVPROPTYPE::default();
    let mut required = 0u32;
    let mut buffer = vec![0u8; 4096];
    SetupDiGetDevicePropertyW(
        set,
        data,
        key,
        &mut property_type,
        Some(&mut buffer),
        Some(&mut required),
        0,
    )
    .map_err(|error| error.to_string())?;
    buffer.truncate(required as usize);
    Ok((property_type, buffer))
}

unsafe fn string_property(
    set: HDEVINFO,
    data: &SP_DEVINFO_DATA,
    key: *const windows::Win32::Foundation::DEVPROPKEY,
) -> Result<String, String> {
    let (property_type, bytes) = device_property(set, data, key)?;
    if property_type != DEVPROP_TYPE_STRING {
        return Err("PnP property is not a string".into());
    }
    Ok(wide_string(&bytes))
}

unsafe fn guid_property(
    set: HDEVINFO,
    data: &SP_DEVINFO_DATA,
    key: *const windows::Win32::Foundation::DEVPROPKEY,
) -> Result<GUID, String> {
    let (property_type, bytes) = device_property(set, data, key)?;
    if property_type != DEVPROP_TYPE_GUID || bytes.len() < size_of::<GUID>() {
        return Err("PnP property is not a GUID".into());
    }
    Ok(std::ptr::read_unaligned(bytes.as_ptr().cast::<GUID>()))
}

fn connected_usb_inventory() -> Result<Vec<DeviceRecord>, String> {
    let set = unsafe {
        SetupDiGetClassDevsW(
            None,
            PCWSTR::null(),
            None,
            DIGCF_ALLCLASSES | DIGCF_PRESENT,
        )
        .map(DeviceInfoSet)
        .map_err(|error| error.to_string())?
    };
    let mut records = Vec::new();
    for index in 0.. {
        let mut data = SP_DEVINFO_DATA {
            cbSize: size_of::<SP_DEVINFO_DATA>() as u32,
            ..Default::default()
        };
        if unsafe { SetupDiEnumDeviceInfo(set.0, index, &mut data) }.is_err() {
            break;
        }
        let mut id = vec![0u16; 4096];
        let mut required = 0u32;
        unsafe {
            SetupDiGetDeviceInstanceIdW(set.0, &data, Some(&mut id), Some(&mut required))
        }
        .map_err(|error| error.to_string())?;
        let instance_id = String::from_utf16_lossy(
            &id[..required.saturating_sub(1) as usize],
        );
        if !instance_id
            .to_ascii_uppercase()
            .starts_with(&USB_PREFIX.to_ascii_uppercase())
        {
            continue;
        }
        let bus_description = unsafe {
            string_property(set.0, &data, &DEVPKEY_Device_BusReportedDeviceDesc)
        }
        .unwrap_or_default();
        let device_description = unsafe {
            string_property(set.0, &data, &DEVPKEY_Device_DeviceDesc)
        }
        .unwrap_or_default();
        let container_id = unsafe {
            guid_property(set.0, &data, &DEVPKEY_Device_ContainerId)
        }?;
        let parent = unsafe { string_property(set.0, &data, &DEVPKEY_Device_Parent) }
            .unwrap_or_default();
        records.push(DeviceRecord {
            instance_id,
            bus_description,
            device_description,
            container_id,
            parent,
            dev_inst: data.DevInst,
        });
    }
    Ok(records)
}

fn registry_path(instance_id: &str) -> Result<String, String> {
    let parts = instance_id.splitn(3, '\\').collect::<Vec<_>>();
    if parts.len() != 3 || !parts[0].eq_ignore_ascii_case("USB") {
        return Err("invalid USB device instance ID".into());
    }
    Ok(format!(
        r"SYSTEM\CurrentControlSet\Enum\USB\{}\{}",
        parts[1], parts[2]
    ))
}

fn optional_string(key: &RegKey, name: &str) -> Result<Option<String>, String> {
    match key.get_value(name) {
        Ok(value) => Ok(Some(value)),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(None),
        Err(error) => Err(error.to_string()),
    }
}

fn registry_names(path: &str) -> Result<(String, Option<String>, Option<String>), String> {
    let hklm = RegKey::predef(HKEY_LOCAL_MACHINE);
    let root = hklm
        .open_subkey_with_flags(path, KEY_READ)
        .map_err(|error| error.to_string())?;
    let parameters = hklm
        .open_subkey_with_flags(format!(r"{}\Device Parameters", path), KEY_READ)
        .map_err(|error| error.to_string())?;
    let port_name: String = parameters
        .get_value("PortName")
        .map_err(|error| error.to_string())?;
    Ok((
        port_name,
        optional_string(&root, "FriendlyName")?,
        optional_string(&parameters, "FriendlyName")?,
    ))
}

fn interface_name(mi: &str) -> Option<&'static str> {
    match mi {
        "MI_02" => Some("MKLink USB to UART"),
        "MI_04" => Some("MKLink Python Console"),
        "MI_06" => Some("MKLink USB to RS485"),
        _ => None,
    }
}

fn probe_version(description: &str) -> Option<u8> {
    let normalized = description
        .to_ascii_uppercase()
        .replace(' ', "")
        .replace('_', "");
    (2..=4).find(|version| {
        normalized.contains(&format!("MICROKEENV{}", version))
            && normalized.contains("CMSIS-DAP")
    })
}

fn candidates(inventory: &[DeviceRecord]) -> Result<(usize, Vec<PortCandidate>), String> {
    let root_prefix = format!(r"{}\", USB_PREFIX);
    let roots = inventory.iter().filter_map(|record| {
        if !record
            .instance_id
            .to_ascii_uppercase()
            .starts_with(&root_prefix.to_ascii_uppercase())
            || record.instance_id[root_prefix.len()..].contains('\\')
        {
            return None;
        }
        let version = probe_version(&record.bus_description)?;
        Some((record, version))
    });

    let mut complete_devices = 0usize;
    let mut result = Vec::new();
    for (root, version) in roots {
        let expected = if version == 4 {
            &["MI_02", "MI_04", "MI_06"][..]
        } else {
            &["MI_02", "MI_04"][..]
        };
        let mut device_ports = Vec::new();
        for mi in expected {
            let description = interface_name(mi).expect("expected MI has a name");
            let prefix = format!(r"{}&{}\", USB_PREFIX, mi);
            let matches = inventory
                .iter()
                .filter(|record| {
                    record
                        .instance_id
                        .to_ascii_uppercase()
                        .starts_with(&prefix.to_ascii_uppercase())
                        && record.parent.eq_ignore_ascii_case(&root.instance_id)
                        && record.container_id == root.container_id
                })
                .collect::<Vec<_>>();
            if matches.len() != 1 {
                device_ports.clear();
                break;
            }
            let record = matches[0];
            let path = registry_path(&record.instance_id)?;
            let (port_name, root_name, parameters_name) = registry_names(&path)?;
            device_ports.push(PortCandidate {
                instance_id: record.instance_id.clone(),
                bus_description: record.bus_description.clone(),
                device_description: record.device_description.clone(),
                container_id: record.container_id,
                parent: record.parent.clone(),
                mi: (*mi).to_string(),
                target_name: format!("{} ({})", description, port_name),
                port_name,
                current_root_name: root_name,
                current_parameters_name: parameters_name,
                registry_path: path,
                dev_inst: record.dev_inst,
            });
        }
        if device_ports.len() == expected.len() {
            complete_devices += 1;
            result.extend(device_ports);
        }
    }
    result.sort_by(|left, right| left.instance_id.cmp(&right.instance_id));
    Ok((complete_devices, result))
}

fn backup_path() -> Result<PathBuf, String> {
    let program_data = std::env::var_os("ProgramData")
        .ok_or_else(|| "ProgramData is unavailable".to_string())?;
    Ok(PathBuf::from(program_data)
        .join("Mklink AI Probe")
        .join("usb-port-name-backup.json"))
}

fn load_backup(path: &Path) -> Result<BackupFile, String> {
    if !path.is_file() {
        return Ok(BackupFile::default());
    }
    let bytes = std::fs::read(path).map_err(|error| error.to_string())?;
    serde_json::from_slice(&bytes).map_err(|error| error.to_string())
}

fn save_backup(path: &Path, backup: &BackupFile) -> Result<(), String> {
    let parent = path
        .parent()
        .ok_or_else(|| "backup path has no parent".to_string())?;
    std::fs::create_dir_all(parent).map_err(|error| error.to_string())?;
    let bytes = serde_json::to_vec_pretty(backup).map_err(|error| error.to_string())?;
    std::fs::write(path, bytes).map_err(|error| error.to_string())
}

fn set_optional_string(key: &RegKey, name: &str, value: &Option<String>) -> Result<(), String> {
    match value {
        Some(value) => key.set_value(name, value).map_err(|error| error.to_string()),
        None => match key.delete_value(name) {
            Ok(()) => Ok(()),
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(()),
            Err(error) => Err(error.to_string()),
        },
    }
}

fn write_names(candidate: &PortCandidate, names: &FriendlyNameBackup) -> Result<(), String> {
    let hklm = RegKey::predef(HKEY_LOCAL_MACHINE);
    let root = hklm
        .open_subkey_with_flags(&candidate.registry_path, KEY_READ | KEY_WRITE)
        .map_err(|error| error.to_string())?;
    let parameters = hklm
        .open_subkey_with_flags(
            format!(r"{}\Device Parameters", candidate.registry_path),
            KEY_READ | KEY_WRITE,
        )
        .map_err(|error| error.to_string())?;
    set_optional_string(&root, "FriendlyName", &names.root)?;
    set_optional_string(&parameters, "FriendlyName", &names.parameters)?;

    let value = names
        .root
        .as_deref()
        .or(names.parameters.as_deref())
        .ok_or_else(|| "FriendlyName cannot be empty".to_string())?;
    let wide = value.encode_utf16().chain(std::iter::once(0)).collect::<Vec<_>>();
    let bytes = unsafe {
        std::slice::from_raw_parts(wide.as_ptr().cast::<u8>(), wide.len() * 2)
    };
    unsafe {
        let result = CM_Set_DevNode_PropertyW(
            candidate.dev_inst,
            &DEVPKEY_Device_FriendlyName,
            DEVPROP_TYPE_STRING,
            Some(bytes),
            0,
        );
        if result != CR_SUCCESS {
            return Err(format!("SetupAPI FriendlyName update failed: {result:?}"));
        }
    }
    Ok(())
}

fn fallback_name(candidate: &PortCandidate) -> String {
    let description = if candidate.device_description.trim().is_empty() {
        "USB Serial Device"
    } else {
        candidate.device_description.trim()
    };
    format!("{} ({})", description, candidate.port_name)
}

pub fn apply(action: &str) -> Result<UsbNamingResult, String> {
    if action != "apply" && action != "restore" {
        return Err("USB port naming action must be apply or restore".into());
    }
    let (matched_devices, preview) = candidates(&connected_usb_inventory()?)?;
    if preview.is_empty() {
        return Ok(UsbNamingResult {
            status: "noDevices".into(),
            matched_devices: 0,
            ports: 0,
            changed: 0,
        });
    }

    let (_, current) = candidates(&connected_usb_inventory()?)?;
    if current != preview {
        return Err("MKLink USB device identity changed before mutation".into());
    }

    let path = backup_path()?;
    let mut backup = load_backup(&path)?;
    let mut changed = Vec::new();
    if action == "apply" {
        for candidate in &current {
            let target = Some(candidate.target_name.clone());
            if candidate.current_root_name == target
                && candidate.current_parameters_name == target
            {
                continue;
            }
            backup
                .entries
                .entry(candidate.instance_id.to_ascii_uppercase())
                .or_insert_with(|| FriendlyNameBackup {
                    root: candidate.current_root_name.clone(),
                    parameters: candidate.current_parameters_name.clone(),
                });
            changed.push((
                candidate,
                FriendlyNameBackup {
                    root: Some(candidate.target_name.clone()),
                    parameters: Some(candidate.target_name.clone()),
                },
            ));
        }
        if !changed.is_empty() {
            save_backup(&path, &backup)?;
        }
    } else {
        for candidate in &current {
            let fallback = fallback_name(candidate);
            let target = backup
                .entries
                .get(&candidate.instance_id.to_ascii_uppercase())
                .and_then(|entry| {
                    match (&entry.root, &entry.parameters) {
                        (Some(root), Some(parameters)) if root == parameters => Some(root.clone()),
                        _ => None,
                    }
                })
                .unwrap_or(fallback);
            let target = Some(target);
            if candidate.current_root_name == target
                && candidate.current_parameters_name == target
            {
                continue;
            }
            changed.push((
                candidate,
                FriendlyNameBackup {
                    root: target.clone(),
                    parameters: target,
                },
            ));
        }
    }

    for (candidate, names) in &changed {
        write_names(candidate, names)?;
    }
    for (candidate, _) in &changed {
        unsafe {
            let _ = CM_Reenumerate_DevNode(candidate.dev_inst, CM_REENUMERATE_SYNCHRONOUS);
        }
    }
    for (candidate, names) in &changed {
        let (_, root, parameters) = registry_names(&candidate.registry_path)?;
        if root != names.root || parameters != names.parameters {
            return Err("MKLink USB registry write verification failed".into());
        }
    }

    Ok(UsbNamingResult {
        status: if action == "apply" {
            "applied".into()
        } else {
            "restored".into()
        },
        matched_devices,
        ports: current.len(),
        changed: changed.len(),
    })
}

#[cfg(test)]
mod tests {
    use super::{fallback_name, probe_version, PortCandidate};
    use windows::core::GUID;

    #[test]
    fn probe_version_accepts_common_windows_descriptor_variants() {
        assert_eq!(probe_version("MicroKeenV3 CMSIS-DAP"), Some(3));
        assert_eq!(probe_version("MicroKeen V4 CMSIS-DAP"), Some(4));
        assert_eq!(probe_version("MICROKEEN_V2 CMSIS-DAP"), Some(2));
        assert_eq!(probe_version("USB Serial Device"), None);
    }

    #[test]
    fn restore_fallback_keeps_the_com_port_visible() {
        let candidate = PortCandidate {
            instance_id: String::new(),
            bus_description: String::new(),
            device_description: "USB Serial Device".into(),
            container_id: GUID::zeroed(),
            parent: String::new(),
            mi: "MI_04".into(),
            port_name: "COM123".into(),
            target_name: "MKLink Python Console (COM123)".into(),
            current_root_name: None,
            current_parameters_name: None,
            registry_path: String::new(),
            dev_inst: 0,
        };
        assert_eq!(fallback_name(&candidate), "USB Serial Device (COM123)");
    }
}
