mod site_agent_config;
mod site_agent_network;
mod site_agent_secret;
#[cfg(target_os = "windows")]
mod usb_port_naming;

use serde::{Deserialize, Serialize};
use std::path::{Path, PathBuf};
use std::process::{Child, Command};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Mutex;
use tauri::menu::{Menu, MenuItem};
use tauri::tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent};
use tauri::{Emitter, Manager, State};

#[cfg(target_os = "windows")]
use windows::core::PCWSTR;
#[cfg(target_os = "windows")]
use windows::Win32::Foundation::{CloseHandle, HANDLE};
#[cfg(target_os = "windows")]
use windows::Win32::System::JobObjects::{
    AssignProcessToJobObject, CreateJobObjectW, SetInformationJobObject,
    JOB_OBJECT_LIMIT_BREAKAWAY_OK, JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
};

/// Thread-safe wrapper for Windows HANDLE — raw pointers are not Send/Sync.
#[cfg(target_os = "windows")]
struct JobHandle(HANDLE);
#[cfg(target_os = "windows")]
unsafe impl Send for JobHandle {}
#[cfg(target_os = "windows")]
unsafe impl Sync for JobHandle {}
#[cfg(target_os = "windows")]
impl Drop for JobHandle {
    fn drop(&mut self) {
        unsafe {
            let _ = CloseHandle(self.0);
        }
    }
}

struct Sidecar {
    child: Mutex<Option<Child>>,
    port: Mutex<Option<u16>>,
    instance_id: String,
    runtime_info_path: PathBuf,
    project_root: Mutex<String>,
    site_agent_root: Mutex<Option<PathBuf>>,
    #[cfg(target_os = "windows")]
    job: Mutex<Option<JobHandle>>,
}

const MAX_RESTARTS: u32 = 5;
const HEALTH_CHECK_INTERVAL_SECS: u64 = 5;
const MAX_CONSECUTIVE_FAILS: u32 = 3;
const DEFAULT_SIDECAR_PORT: u16 = 8765;
const LAST_SIDECAR_PORT: u16 = 8799;
const SIDECAR_START_TIMEOUT_SECS: u64 = 20;
const SIDECAR_SHUTDOWN_TIMEOUT_SECS: u64 = 2;

#[derive(Debug, Clone, PartialEq, Eq, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
struct BackendEndpoint {
    port: u16,
    instance_id: String,
}

#[derive(Debug, Clone, PartialEq, Eq)]
enum SidecarLaunch {
    Bundled(std::path::PathBuf),
    Python(String),
}

fn choose_sidecar_launch(
    bundled: Option<std::path::PathBuf>,
    python: Option<String>,
) -> Result<SidecarLaunch, String> {
    if let Some(path) = bundled {
        return Ok(SidecarLaunch::Bundled(path));
    }
    if let Some(command) = python {
        return Ok(SidecarLaunch::Python(command));
    }
    Err("No bundled sidecar or Python runtime is available".into())
}

fn find_bundled_sidecar() -> Option<std::path::PathBuf> {
    let directory = std::env::current_exe().ok()?.parent()?.to_path_buf();
    let candidate = directory.join("mklink-sidecar.exe");
    candidate.is_file().then_some(candidate)
}

fn find_python() -> Option<String> {
    for name in &["python", "python3"] {
        if which_exists(name) {
            return Some(name.to_string());
        }
    }
    None
}

fn resolve_sidecar_launch() -> Result<SidecarLaunch, String> {
    choose_sidecar_launch(find_bundled_sidecar(), find_python())
}

fn default_project_root() -> String {
    ".".into()
}

fn desktop_workspace_root(app_data_dir: &Path) -> PathBuf {
    app_data_dir.join("workspace")
}

fn configured_site_agent_root(state: &Sidecar) -> Result<PathBuf, String> {
    state
        .site_agent_root
        .lock()
        .map_err(|error| error.to_string())?
        .clone()
        .ok_or_else(|| "Site Agent storage is not initialized".to_string())
}

fn apply_site_agent_environment(command: &mut Command, root: Option<&Path>) {
    const VARIABLES: &[&str] = &[
        "MKLINK_SITE_AGENT_ENABLED",
        "MKLINK_SITE_AGENT_HOST",
        "MKLINK_SITE_AGENT_PORT",
        "MKLINK_SITE_AGENT_ALLOW_LAN",
        "MKLINK_SITE_AGENT_TRANSPORT",
        "MKLINK_SITE_AGENT_CONFIGURATION_ERROR",
        "MKLINK_REMOTE_TOKEN",
        "MKLINK_STCP_SERVER_ADDR",
        "MKLINK_STCP_SERVER_PORT",
        "MKLINK_STCP_USER",
        "MKLINK_STCP_PROXY_NAME",
        "MKLINK_STCP_AUTH_TOKEN",
        "MKLINK_STCP_SECRET",
        "MKLINK_STCP_LIBRARY",
    ];
    for name in VARIABLES {
        command.env_remove(name);
    }
    let Some(root) = root else {
        command.env("MKLINK_SITE_AGENT_ENABLED", "0");
        return;
    };
    let prepared = (|| {
        let config = site_agent_config::load(root)?;
        if !config.enabled {
            return Ok::<_, String>((config, None, None));
        }
        let token = site_agent_secret::load(root)?;
        let stcp = if config.transport == "lan-stcp" {
            Some(site_agent_secret::load_stcp(root)?)
        } else {
            None
        };
        config.validate(true, stcp.is_some())?;
        if !site_agent_network::is_local_bind(&config.bind_host) {
            return Err("The configured Site Agent bind address is not active".into());
        }
        if let Some(credentials) = stcp.as_ref() {
            if credentials.auth_token == token || credentials.secret_key == token {
                return Err("Site Agent and STCP credentials must be distinct".into());
            }
        }
        Ok((config, Some(token), stcp))
    })();
    let (config, token, stcp) = match prepared {
        Ok(value) => value,
        Err(error) => {
            eprintln!("[tauri] Site Agent configuration disabled: {error}");
            command.env("MKLINK_SITE_AGENT_ENABLED", "0").env(
                "MKLINK_SITE_AGENT_CONFIGURATION_ERROR",
                "Site Agent configuration or credentials are invalid",
            );
            return;
        }
    };
    if !config.enabled {
        command.env("MKLINK_SITE_AGENT_ENABLED", "0");
        return;
    }
    command
        .env("MKLINK_SITE_AGENT_ENABLED", "1")
        .env("MKLINK_SITE_AGENT_HOST", &config.bind_host)
        .env("MKLINK_SITE_AGENT_PORT", config.port.to_string())
        .env(
            "MKLINK_SITE_AGENT_ALLOW_LAN",
            if config.allow_lan { "1" } else { "0" },
        )
        .env("MKLINK_SITE_AGENT_TRANSPORT", &config.transport)
        .env(
            "MKLINK_REMOTE_TOKEN",
            token.expect("enabled configuration has a token"),
        );
    if let Some(credentials) = stcp {
        command
            .env("MKLINK_STCP_SERVER_ADDR", &config.stcp_server_addr)
            .env(
                "MKLINK_STCP_SERVER_PORT",
                config.stcp_server_port.to_string(),
            )
            .env("MKLINK_STCP_USER", &config.stcp_user)
            .env("MKLINK_STCP_PROXY_NAME", &config.stcp_proxy_name)
            .env("MKLINK_STCP_AUTH_TOKEN", credentials.auth_token)
            .env("MKLINK_STCP_SECRET", credentials.secret_key);
    }
}

#[tauri::command]
fn site_agent_config_get(
    state: State<Sidecar>,
) -> Result<site_agent_config::SiteAgentConfig, String> {
    site_agent_config::load(&configured_site_agent_root(state.inner())?)
}

#[tauri::command]
fn site_agent_config_save(
    config: site_agent_config::SiteAgentConfig,
    state: State<Sidecar>,
) -> Result<bool, String> {
    let root = configured_site_agent_root(state.inner())?;
    config.validate(
        site_agent_secret::configured(&root),
        site_agent_secret::stcp_configured(&root),
    )?;
    if config.enabled && !site_agent_network::is_local_bind(&config.bind_host) {
        return Err("The selected Site Agent bind address is not active on this host".into());
    }
    let previous = site_agent_config::load(&root)?;
    let restart_required =
        serde_json::to_value(&previous).ok() != serde_json::to_value(&config).ok();
    site_agent_config::save(&root, &config)?;
    Ok(restart_required)
}

#[tauri::command]
fn site_agent_secret_state(
    state: State<Sidecar>,
) -> Result<site_agent_secret::SecretState, String> {
    Ok(site_agent_secret::state(&configured_site_agent_root(
        state.inner(),
    )?))
}

#[tauri::command]
fn site_agent_generate_token_and_copy(
    state: State<Sidecar>,
) -> Result<site_agent_secret::TokenResult, String> {
    site_agent_secret::generate_and_copy(&configured_site_agent_root(state.inner())?)
}

#[tauri::command]
fn site_agent_stcp_credentials_configure(
    auth_token: String,
    secret_key: String,
    state: State<Sidecar>,
) -> Result<(), String> {
    site_agent_secret::store_stcp(
        &configured_site_agent_root(state.inner())?,
        &auth_token,
        &secret_key,
    )
}

#[tauri::command]
fn site_agent_bind_addresses() -> Vec<String> {
    site_agent_network::local_bind_addresses()
}

#[tauri::command]
fn write_file(path: String, contents: Vec<u8>) -> Result<(), String> {
    let target = PathBuf::from(path);
    if target.as_os_str().is_empty() {
        return Err("file path is empty".into());
    }
    std::fs::write(&target, contents).map_err(|error| error.to_string())
}

fn powershell_single_quote(value: &str) -> String {
    format!("'{}'", value.replace('\'', "''"))
}

fn elevated_helper_arguments(executable: &Path, operation: &str) -> (String, String) {
    let executable = executable.to_string_lossy().into_owned();
    let arguments = format!("--manage-usb-port-names {}", operation);
    (executable, arguments)
}

#[tauri::command]
fn rename_usb_ports(action: String) -> Result<serde_json::Value, String> {
    if action != "apply" && action != "restore" {
        return Err("USB port naming action must be apply or restore".into());
    }

    #[cfg(not(target_os = "windows"))]
    {
        return Err("USB port naming is only available on Windows".into());
    }

    #[cfg(target_os = "windows")]
    {
        use std::os::windows::process::CommandExt;
        const CREATE_NO_WINDOW: u32 = 0x08000000;

        let executable = std::env::current_exe()
            .map_err(|error| format!("cannot resolve desktop executable: {error}"))?;
        let (file_path, child_arguments) = elevated_helper_arguments(&executable, &action);
        // The helper itself performs identity validation, registry backup and
        // write verification. Start-Process -Verb RunAs supplies the UAC
        // boundary when the desktop app is running as a normal user.
        let command = format!(
            "$p = Start-Process -FilePath {} -ArgumentList {} -Verb RunAs -Wait -PassThru; exit $p.ExitCode",
            powershell_single_quote(&file_path),
            powershell_single_quote(&child_arguments),
        );
        let status = Command::new("powershell.exe")
            .creation_flags(CREATE_NO_WINDOW)
            .args(["-NoProfile", "-NonInteractive", "-Command", &command])
            .status()
            .map_err(|error| format!("failed to start USB naming helper: {error}"))?;
        let code = status.code().unwrap_or(1);
        if code != 0 {
            return Err(format!("USB port naming helper exited with code {code}"));
        }
        Ok(serde_json::json!({ "action": action, "status": "completed" }))
    }
}

#[cfg(target_os = "windows")]
pub fn run_usb_port_naming_cli(action: &str) -> Result<serde_json::Value, String> {
    serde_json::to_value(usb_port_naming::apply(action)?).map_err(|error| error.to_string())
}

#[cfg(target_os = "windows")]
fn which_exists(name: &str) -> bool {
    use std::os::windows::process::CommandExt;
    const CREATE_NO_WINDOW: u32 = 0x08000000;
    Command::new("where")
        .arg(name)
        .creation_flags(CREATE_NO_WINDOW)
        .output()
        .map(|o| o.status.success())
        .unwrap_or(false)
}

#[cfg(not(target_os = "windows"))]
fn which_exists(name: &str) -> bool {
    Command::new("which")
        .arg(name)
        .output()
        .map(|o| o.status.success())
        .unwrap_or(false)
}

/// Create a Windows Job Object that kills child processes when the parent dies.
#[cfg(target_os = "windows")]
fn create_kill_on_close_job() -> Result<JobHandle, String> {
    use windows::Win32::System::JobObjects::JobObjectExtendedLimitInformation;

    unsafe {
        let job = CreateJobObjectW(None, PCWSTR::null())
            .map_err(|e| format!("CreateJobObjectW failed: {}", e))?;

        let mut info =
            windows::Win32::System::JobObjects::JOBOBJECT_EXTENDED_LIMIT_INFORMATION::default();
        info.BasicLimitInformation.LimitFlags =
            JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE | JOB_OBJECT_LIMIT_BREAKAWAY_OK;

        SetInformationJobObject(
            job,
            JobObjectExtendedLimitInformation,
            &info as *const _ as *const _,
            std::mem::size_of::<
                windows::Win32::System::JobObjects::JOBOBJECT_EXTENDED_LIMIT_INFORMATION,
            >() as u32,
        )
        .map_err(|e| format!("SetInformationJobObject failed: {}", e))?;

        Ok(JobHandle(job))
    }
}

/// Assign a child process to the job object so it dies when we die.
#[cfg(target_os = "windows")]
fn assign_to_job(job: &JobHandle, child: &Child) -> Result<(), String> {
    use windows::Win32::System::Threading::{OpenProcess, PROCESS_ALL_ACCESS};

    unsafe {
        let proc_handle = OpenProcess(PROCESS_ALL_ACCESS, false, child.id())
            .map_err(|e| format!("OpenProcess({}) failed: {}", child.id(), e))?;

        AssignProcessToJobObject(job.0, proc_handle)
            .map_err(|e| format!("AssignProcessToJobObject failed: {}", e))?;

        let _ = CloseHandle(proc_handle);
        Ok(())
    }
}

fn spawn_sidecar(
    launch: &SidecarLaunch,
    port: u16,
    instance_id: &str,
    runtime_info_path: &Path,
    project_root: &str,
    site_agent_root: Option<&Path>,
) -> Result<Child, String> {
    use std::os::windows::process::CommandExt;
    use std::process::Stdio;
    // CREATE_NO_WINDOW = 0x08000000 — prevents Python console window from flashing
    const CREATE_NO_WINDOW: u32 = 0x08000000;

    let (mut command, label) = match launch {
        SidecarLaunch::Bundled(path) => (Command::new(path), path.to_string_lossy().into_owned()),
        SidecarLaunch::Python(python) => {
            let mut command = Command::new(python);
            command.args(["-m", "mklink"]);
            (command, python.clone())
        }
    };

    command
        .args([
            "serve",
            "--host",
            "127.0.0.1",
            "--port",
            &port.to_string(),
            "--desktop-port-end",
            &LAST_SIDECAR_PORT.to_string(),
            "--desktop-runtime-info",
            &runtime_info_path.to_string_lossy(),
            "--desktop-instance-id",
            instance_id,
            "--project-root",
            project_root,
        ])
        .env("MKLINK_PARENT_JOB_BREAKAWAY_OK", "1");
    apply_site_agent_environment(&mut command, site_agent_root);
    if let SidecarLaunch::Bundled(path) = launch {
        let stcp_library = path
            .parent()
            .unwrap_or_else(|| Path::new("."))
            .join("mklink-stcp.dll");
        if stcp_library.is_file() {
            command.env("MKLINK_STCP_LIBRARY", stcp_library);
        }
    }
    command
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .creation_flags(CREATE_NO_WINDOW)
        .spawn()
        .map_err(|e| format!("Failed to start sidecar ({}): {}", label, e))
}

fn retain_child_if_registered<T, E>(
    mut child: T,
    register: impl FnOnce(&T) -> Result<(), E>,
    cleanup: impl FnOnce(&mut T),
) -> Result<T, E> {
    if let Err(error) = register(&child) {
        cleanup(&mut child);
        return Err(error);
    }
    Ok(child)
}

#[cfg(target_os = "windows")]
fn spawn_registered_sidecar(
    state: &Sidecar,
    launch: &SidecarLaunch,
    port: u16,
    project_root: &str,
) -> Result<Child, String> {
    let mut job_guard = state.job.lock().map_err(|e| e.to_string())?;
    if job_guard.is_none() {
        *job_guard = Some(create_kill_on_close_job()?);
    }
    let job = job_guard.as_ref().expect("job was initialized");
    let site_agent_root = state
        .site_agent_root
        .lock()
        .map_err(|error| error.to_string())?
        .clone();
    let child = spawn_sidecar(
        launch,
        port,
        &state.instance_id,
        &state.runtime_info_path,
        project_root,
        site_agent_root.as_deref(),
    )?;
    retain_child_if_registered(
        child,
        |child| assign_to_job(job, child),
        |child| {
            let _ = child.kill();
            let _ = child.wait();
        },
    )
}

fn request_sidecar_shutdown(port: u16, instance_id: &str) -> bool {
    use std::io::{Read, Write};

    let addr = format!("127.0.0.1:{}", port);
    let mut stream = match std::net::TcpStream::connect_timeout(
        &addr.parse().unwrap(),
        std::time::Duration::from_millis(500),
    ) {
        Ok(stream) => stream,
        Err(_) => return false,
    };
    let timeout = std::time::Duration::from_secs(SIDECAR_SHUTDOWN_TIMEOUT_SECS);
    let _ = stream.set_read_timeout(Some(timeout));
    let _ = stream.set_write_timeout(Some(timeout));

    let body = serde_json::json!({ "instance_id": instance_id }).to_string();
    let request = format!(
        "POST /api/desktop/shutdown HTTP/1.1\r\nHost: 127.0.0.1:{}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
        port,
        body.len(),
        body,
    );
    if stream.write_all(request.as_bytes()).is_err() {
        return false;
    }
    let mut response = Vec::new();
    if stream.take(8192).read_to_end(&mut response).is_err() {
        return false;
    }
    String::from_utf8_lossy(&response)
        .lines()
        .next()
        .is_some_and(|line| line.contains(" 200 "))
}

fn wait_for_child_exit(child: &mut Child) -> bool {
    let deadline =
        std::time::Instant::now() + std::time::Duration::from_secs(SIDECAR_SHUTDOWN_TIMEOUT_SECS);
    while std::time::Instant::now() < deadline {
        match child.try_wait() {
            Ok(Some(_)) => return true,
            Ok(None) => std::thread::sleep(std::time::Duration::from_millis(50)),
            Err(_) => return false,
        }
    }
    child.try_wait().is_ok_and(|status| status.is_some())
}

fn terminate_sidecar_tree(state: &Sidecar) -> Result<(), String> {
    let port = *state.port.lock().map_err(|e| e.to_string())?;
    let mut child = state.child.lock().map_err(|e| e.to_string())?.take();
    let graceful_exit = port.is_some_and(|port| {
        request_sidecar_shutdown(port, &state.instance_id)
            && child.as_mut().is_none_or(wait_for_child_exit)
    });
    *state.port.lock().map_err(|e| e.to_string())? = None;
    let _ = std::fs::remove_file(&state.runtime_info_path);

    // A PyInstaller onefile executable can leave the actual Python worker in
    // the app-owned Job after its tracked launcher exits. Closing the Job is
    // the authoritative way to terminate every process owned by this sidecar
    // generation before starting another one.
    #[cfg(target_os = "windows")]
    {
        let job = state.job.lock().map_err(|e| e.to_string())?.take();
        drop(job);
    }

    if let Some(mut child) = child {
        if !graceful_exit {
            let _ = child.kill();
        }
        let _ = child.wait();
    }
    Ok(())
}

/// Minimal owned-backend health check using raw TCP — no external deps needed.
fn check_health(port: u16, instance_id: &str) -> bool {
    use std::io::{Read, Write};
    let addr = format!("127.0.0.1:{}", port);
    let mut stream = match std::net::TcpStream::connect_timeout(
        &addr.parse().unwrap(),
        std::time::Duration::from_secs(3),
    ) {
        Ok(s) => s,
        Err(_) => return false,
    };
    let _ = stream.set_read_timeout(Some(std::time::Duration::from_secs(3)));
    let _ = stream.set_write_timeout(Some(std::time::Duration::from_secs(3)));

    let request = format!(
        "GET /api/health HTTP/1.1\r\nHost: 127.0.0.1:{}\r\nConnection: close\r\n\r\n",
        port
    );
    if stream.write_all(request.as_bytes()).is_err() {
        return false;
    }
    let mut response = Vec::new();
    if stream.take(8192).read_to_end(&mut response).is_err() {
        return false;
    }
    let response = String::from_utf8_lossy(&response);
    health_response_matches(&response, instance_id)
}

fn health_response_matches(response: &str, instance_id: &str) -> bool {
    let Some((headers, body)) = response.split_once("\r\n\r\n") else {
        return false;
    };
    if !headers
        .lines()
        .next()
        .is_some_and(|line| line.contains(" 200 "))
    {
        return false;
    }
    serde_json::from_str::<serde_json::Value>(body).is_ok_and(|payload| {
        payload.get("status").and_then(|value| value.as_str()) == Some("ok")
            && payload
                .get("desktop_instance_id")
                .and_then(|value| value.as_str())
                == Some(instance_id)
    })
}

fn current_endpoint(state: &Sidecar) -> Result<Option<BackendEndpoint>, String> {
    Ok(state
        .port
        .lock()
        .map_err(|error| error.to_string())?
        .map(|port| BackendEndpoint {
            port,
            instance_id: state.instance_id.clone(),
        }))
}

fn wait_for_runtime_endpoint(state: &Sidecar) -> Result<BackendEndpoint, String> {
    let deadline =
        std::time::Instant::now() + std::time::Duration::from_secs(SIDECAR_START_TIMEOUT_SECS);
    while std::time::Instant::now() < deadline {
        if let Ok(raw) = std::fs::read_to_string(&state.runtime_info_path) {
            if let Ok(endpoint) = serde_json::from_str::<BackendEndpoint>(&raw) {
                if endpoint.instance_id == state.instance_id
                    && (DEFAULT_SIDECAR_PORT..=LAST_SIDECAR_PORT).contains(&endpoint.port)
                {
                    *state.port.lock().map_err(|error| error.to_string())? = Some(endpoint.port);
                    return Ok(endpoint);
                }
            }
        }
        let exited = {
            let mut guard = state.child.lock().map_err(|error| error.to_string())?;
            match guard.as_mut() {
                Some(child) => child
                    .try_wait()
                    .map_err(|error| error.to_string())?
                    .is_some(),
                None => true,
            }
        };
        if exited {
            return Err("The sidecar exited before publishing its API endpoint".into());
        }
        std::thread::sleep(std::time::Duration::from_millis(100));
    }
    Err("Timed out waiting for the sidecar API endpoint".into())
}

fn start_owned_sidecar(
    state: &Sidecar,
    project_root: Option<String>,
    preferred_port: Option<u16>,
) -> Result<BackendEndpoint, String> {
    let process_alive = {
        let mut guard = state.child.lock().map_err(|error| error.to_string())?;
        match guard.as_mut() {
            Some(child) => child
                .try_wait()
                .map(|status| status.is_none())
                .unwrap_or(false),
            None => false,
        }
    };
    if process_alive {
        if let Some(endpoint) = current_endpoint(state)? {
            return Ok(endpoint);
        }
        return wait_for_runtime_endpoint(state);
    }

    terminate_sidecar_tree(state)?;
    let port = preferred_port.unwrap_or(DEFAULT_SIDECAR_PORT);
    if !(DEFAULT_SIDECAR_PORT..=LAST_SIDECAR_PORT).contains(&port) {
        return Err("The preferred sidecar port is outside the desktop range".into());
    }
    let project_root = match project_root {
        Some(path) => path,
        None => state
            .project_root
            .lock()
            .map_err(|error| error.to_string())?
            .clone(),
    };
    let launch = resolve_sidecar_launch()?;
    let child = spawn_registered_sidecar(state, &launch, port, &project_root)?;
    *state.child.lock().map_err(|error| error.to_string())? = Some(child);

    match wait_for_runtime_endpoint(state) {
        Ok(endpoint) => Ok(endpoint),
        Err(error) => {
            let _ = terminate_sidecar_tree(state);
            Err(error)
        }
    }
}

#[tauri::command]
fn sidecar_status(state: State<Sidecar>) -> Result<bool, String> {
    let mut guard = state.child.lock().map_err(|e| e.to_string())?;
    match guard.as_mut() {
        Some(child) => match child.try_wait() {
            Ok(Some(_)) => {
                *guard = None;
                Ok(false)
            }
            Ok(None) => Ok(true),
            Err(e) => Err(e.to_string()),
        },
        None => Ok(false),
    }
}

#[tauri::command]
fn start_sidecar(
    state: State<Sidecar>,
    project_root: Option<String>,
) -> Result<BackendEndpoint, String> {
    start_owned_sidecar(state.inner(), project_root, None)
}

#[tauri::command]
fn stop_sidecar(state: State<Sidecar>) -> Result<(), String> {
    terminate_sidecar_tree(state.inner())
}

#[tauri::command]
fn restart_sidecar(state: State<Sidecar>) -> Result<BackendEndpoint, String> {
    let preferred_port = state.port.lock().map_err(|e| e.to_string())?.to_owned();
    terminate_sidecar_tree(state.inner())?;
    start_owned_sidecar(state.inner(), None, preferred_port)
}

#[tauri::command]
fn backend_endpoint(state: State<Sidecar>) -> Result<Option<BackendEndpoint>, String> {
    current_endpoint(state.inner())
}

#[tauri::command]
fn backend_alive(state: State<Sidecar>) -> Result<bool, String> {
    let running = {
        let mut guard = state.child.lock().map_err(|e| e.to_string())?;
        match guard.as_mut() {
            Some(child) => child.try_wait().map(|s| s.is_none()).unwrap_or(false),
            None => false,
        }
    };
    if !running {
        return Ok(false);
    }
    let Some(endpoint) = current_endpoint(state.inner())? else {
        return Ok(false);
    };
    Ok(check_health(endpoint.port, &endpoint.instance_id))
}

fn run_monitor(handle: tauri::AppHandle, shutdown: std::sync::Arc<AtomicBool>) {
    let mut consecutive_fails: u32 = 0;
    let mut restart_count: u32 = 0;

    loop {
        std::thread::sleep(std::time::Duration::from_secs(HEALTH_CHECK_INTERVAL_SECS));

        if shutdown.load(Ordering::Relaxed) {
            break;
        }

        let state: State<Sidecar> = handle.state();

        let process_alive = {
            let mut guard = state.child.lock().unwrap();
            match guard.as_mut() {
                Some(child) => child.try_wait().map(|s| s.is_none()).unwrap_or(false),
                None => false,
            }
        };

        if !process_alive {
            if restart_count >= MAX_RESTARTS {
                eprintln!(
                    "[tauri] max restarts ({}) reached, stopping monitor",
                    MAX_RESTARTS
                );
                break;
            }
            restart_count += 1;
            eprintln!(
                "[tauri] sidecar exited, restarting ({}/{})...",
                restart_count, MAX_RESTARTS
            );
            // Longer backoff: 3s base + 2s per restart attempt
            let backoff = 3 + 2 * (restart_count - 1);
            std::thread::sleep(std::time::Duration::from_secs(backoff as u64));
            let preferred_port = state.port.lock().ok().and_then(|port| *port);
            match start_owned_sidecar(state.inner(), None, preferred_port) {
                Ok(endpoint) => {
                    let _ = handle.emit("backend-endpoint-changed", &endpoint);
                    eprintln!("[tauri] sidecar restarted on port {}", endpoint.port);
                    consecutive_fails = 0;
                }
                Err(e) => eprintln!("[tauri] restart failed: {}", e),
            }
            continue;
        }

        let endpoint = current_endpoint(state.inner()).ok().flatten();
        if endpoint
            .as_ref()
            .is_some_and(|endpoint| check_health(endpoint.port, &endpoint.instance_id))
        {
            consecutive_fails = 0;
        } else {
            consecutive_fails += 1;
            eprintln!(
                "[tauri] health check failed ({}/{})",
                consecutive_fails, MAX_CONSECUTIVE_FAILS
            );
            if consecutive_fails >= MAX_CONSECUTIVE_FAILS {
                eprintln!("[tauri] backend unresponsive, killing...");
                let _ = terminate_sidecar_tree(state.inner());
                consecutive_fails = 0;
            }
        }
    }
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let shutdown = std::sync::Arc::new(AtomicBool::new(false));
    let instance_id = format!("{:032x}", rand::random::<u128>());
    let runtime_info_path = std::env::temp_dir().join(format!(
        "mklink-ai-probe-runtime-{}-{}.json",
        std::process::id(),
        instance_id
    ));

    tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_updater::Builder::new().build())
        .plugin(tauri_plugin_process::init())
        .manage(Sidecar {
            child: Mutex::new(None),
            port: Mutex::new(None),
            instance_id,
            runtime_info_path,
            project_root: Mutex::new(default_project_root()),
            site_agent_root: Mutex::new(None),
            #[cfg(target_os = "windows")]
            job: Mutex::new(None),
        })
        .invoke_handler(tauri::generate_handler![
            sidecar_status,
            start_sidecar,
            stop_sidecar,
            restart_sidecar,
            backend_endpoint,
            backend_alive,
            site_agent_config_get,
            site_agent_config_save,
            site_agent_secret_state,
            site_agent_generate_token_and_copy,
            site_agent_stcp_credentials_configure,
            site_agent_bind_addresses,
            write_file,
            rename_usb_ports,
        ])
        .setup(move |app| {
            let workspace_root = desktop_workspace_root(&app.path().app_local_data_dir()?);
            std::fs::create_dir_all(&workspace_root)?;
            {
                let state: State<Sidecar> = app.state();
                *state
                    .project_root
                    .lock()
                    .map_err(|error| error.to_string())? = workspace_root.to_string_lossy().into_owned();
            }
            let site_agent_root = app.path().app_local_data_dir()?.join("site-agent");
            site_agent_config::ensure_root(&site_agent_root)?;
            {
                let state: State<Sidecar> = app.state();
                *state
                    .site_agent_root
                    .lock()
                    .map_err(|error| error.to_string())? = Some(site_agent_root);
            }
            let handle = app.handle().clone();
            let shutdown_clone = shutdown.clone();

            let show_item = MenuItem::with_id(app, "show", "Show MKLink", true, None::<&str>)?;
            let exit_item = MenuItem::with_id(app, "exit", "Exit", true, None::<&str>)?;
            let tray_menu = Menu::with_items(app, &[&show_item, &exit_item])?;
            let tray_shutdown = shutdown.clone();
            // TrayIconBuilder does not inherit the window icon automatically.
            // Reuse the generated bundle icon so Windows never creates an
            // empty tray item when the app starts from the installer.
            let tray_icon = app
                .default_window_icon()
                .cloned()
                .map(tauri::image::Image::to_owned)
                .ok_or_else(|| {
                    std::io::Error::new(
                        std::io::ErrorKind::NotFound,
                        "Tauri default window icon is missing",
                    )
                })?;
            TrayIconBuilder::new()
                .icon(tray_icon)
                .menu(&tray_menu)
                .show_menu_on_left_click(false)
                .on_menu_event(move |app, event| match event.id.as_ref() {
                    "show" => {
                        if let Some(window) = app.get_webview_window("main") {
                            let _ = window.show();
                            let _ = window.set_focus();
                        }
                    }
                    "exit" => {
                        tray_shutdown.store(true, Ordering::Relaxed);
                        let state: State<Sidecar> = app.state();
                        let _ = terminate_sidecar_tree(state.inner());
                        app.exit(0);
                    }
                    _ => {}
                })
                .on_tray_icon_event(|tray, event| {
                    if let TrayIconEvent::Click {
                        button: MouseButton::Left,
                        button_state: MouseButtonState::Up,
                        ..
                    } = event
                    {
                        let app = tray.app_handle();
                        if let Some(window) = app.get_webview_window("main") {
                            let _ = window.show();
                            let _ = window.set_focus();
                        }
                    }
                })
                .build(app)?;

            // Closing the main window is a real application exit. The sidecar
            // owns the Device and its serial/HIL locks, so it must be stopped
            // even when Site Agent is configured. Users can start the desktop
            // app again when they need the agent; a hidden window must never
            // leave a probe locked unexpectedly.
            let cleanup_handle = app.handle().clone();
            let cleanup_shutdown = shutdown.clone();
            if let Some(window) = app.get_webview_window("main") {
                let _ = window.on_window_event(move |event| {
                    if let tauri::WindowEvent::CloseRequested { api: _, .. } = event {
                        let state: State<Sidecar> = cleanup_handle.state();
                        // The close request is allowed to continue after the
                        // owned sidecar has been asked to shut down. This
                        // keeps the serial release on the same synchronous
                        // path as tray Exit and process shutdown.
                        eprintln!("[tauri] window closing, cleaning up sidecar...");
                        cleanup_shutdown.store(true, Ordering::Relaxed);
                        if terminate_sidecar_tree(state.inner()).is_ok() {
                            eprintln!("[tauri] sidecar killed");
                        }
                    }
                });
            }

            std::thread::spawn(move || {
                let state: State<Sidecar> = handle.state();
                match start_owned_sidecar(state.inner(), None, None) {
                    Ok(endpoint) => {
                        eprintln!("[tauri] sidecar started on port {}", endpoint.port);
                        // Wait for Python to fully initialize before monitoring
                        for _ in 0..20 {
                            if check_health(endpoint.port, &endpoint.instance_id) {
                                eprintln!("[tauri] backend healthy");
                                break;
                            }
                            std::thread::sleep(std::time::Duration::from_millis(500));
                        }
                    }
                    Err(e) => eprintln!(
                        "[tauri] sidecar failed: {} (start Python backend manually)",
                        e
                    ),
                }
                run_monitor(handle, shutdown_clone);
            });

            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn bundled_sidecar_wins_over_python() {
        let bundled = std::path::PathBuf::from(r"C:\Program Files\Mklink\mklink-sidecar.exe");
        assert_eq!(
            choose_sidecar_launch(Some(bundled.clone()), Some("python".into()),).unwrap(),
            SidecarLaunch::Bundled(bundled),
        );
    }

    #[test]
    fn python_is_only_the_development_fallback() {
        assert_eq!(
            choose_sidecar_launch(None, Some("python".into())).unwrap(),
            SidecarLaunch::Python("python".into()),
        );
    }

    #[test]
    fn missing_sidecar_and_python_is_an_error() {
        assert!(choose_sidecar_launch(None, None)
            .unwrap_err()
            .contains("No bundled sidecar or Python runtime"));
    }

    #[test]
    fn installed_runtime_lets_backend_restore_the_last_project() {
        assert_eq!(default_project_root(), ".");
    }

    #[test]
    fn installed_runtime_uses_a_user_writable_workspace() {
        assert_eq!(
            desktop_workspace_root(Path::new(r"C:\Users\test\AppData\Local\Mklink AI Probe")),
            PathBuf::from(r"C:\Users\test\AppData\Local\Mklink AI Probe\workspace"),
        );
    }

    #[test]
    fn powershell_paths_are_single_quote_escaped() {
        assert_eq!(
            powershell_single_quote(r"C:\Program Files\Owner's MKLink\rename.ps1"),
            r"'C:\Program Files\Owner''s MKLink\rename.ps1'"
        );
        assert_eq!(
            elevated_helper_arguments(
                Path::new(r"C:\Program Files\Mklink AI Probe\resources\rename.ps1"),
                "restore",
            ),
            (
                r"C:\Program Files\Mklink AI Probe\resources\rename.ps1".into(),
                "--manage-usb-port-names restore".into(),
            ),
        );
    }

    #[test]
    fn health_check_requires_the_owning_desktop_instance() {
        let response = concat!(
            "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n\r\n",
            r#"{"status":"ok","desktop_instance_id":"instance-a"}"#,
        );
        assert!(health_response_matches(response, "instance-a"));
        assert!(!health_response_matches(response, "instance-b"));
        assert!(!health_response_matches(
            "HTTP/1.1 200 OK\r\n\r\n{\"status\":\"ok\"}",
            "instance-a"
        ));
    }

    #[test]
    fn runtime_endpoint_uses_the_frontend_field_names() {
        let endpoint: BackendEndpoint =
            serde_json::from_str(r#"{"port":8766,"instanceId":"instance-b"}"#).unwrap();
        assert_eq!(endpoint.port, 8766);
        assert_eq!(endpoint.instance_id, "instance-b");
    }

    #[test]
    fn failed_child_registration_runs_cleanup() {
        let mut cleaned = false;
        let result = retain_child_if_registered(
            "child",
            |_| Err("job assignment failed"),
            |_| cleaned = true,
        );

        assert_eq!(result.unwrap_err(), "job assignment failed");
        assert!(cleaned);
    }

    #[test]
    fn successful_child_registration_retains_child_without_cleanup() {
        let mut cleaned = false;
        let result = retain_child_if_registered("child", |_| Ok::<_, &str>(()), |_| cleaned = true);

        assert_eq!(result.unwrap(), "child");
        assert!(!cleaned);
    }

    #[cfg(target_os = "windows")]
    #[test]
    fn terminating_sidecar_closes_job_and_kills_untracked_worker() {
        use std::os::windows::process::CommandExt;
        const CREATE_NO_WINDOW: u32 = 0x08000000;

        fn sleeping_child() -> Child {
            Command::new("cmd.exe")
                .args(["/D", "/C", "ping -n 60 127.0.0.1 >NUL"])
                .creation_flags(CREATE_NO_WINDOW)
                .spawn()
                .expect("spawn sleeping child")
        }

        let job = create_kill_on_close_job().expect("create job");
        let tracked = sleeping_child();
        assign_to_job(&job, &tracked).expect("assign tracked child");
        let mut worker = sleeping_child();
        assign_to_job(&job, &worker).expect("assign untracked worker");

        let state = Sidecar {
            child: Mutex::new(Some(tracked)),
            port: Mutex::new(Some(DEFAULT_SIDECAR_PORT)),
            instance_id: "test-instance".into(),
            runtime_info_path: std::env::temp_dir().join("mklink-test-runtime-info.json"),
            project_root: Mutex::new(default_project_root()),
            site_agent_root: Mutex::new(None),
            job: Mutex::new(Some(job)),
        };

        terminate_sidecar_tree(&state).expect("terminate sidecar tree");
        assert!(state.child.lock().unwrap().is_none());
        assert!(state.job.lock().unwrap().is_none());
        assert!(state.port.lock().unwrap().is_none());

        for _ in 0..40 {
            if worker.try_wait().expect("poll worker").is_some() {
                return;
            }
            std::thread::sleep(std::time::Duration::from_millis(50));
        }
        let _ = worker.kill();
        let _ = worker.wait();
        panic!("untracked worker survived closing the app-owned job");
    }
}
