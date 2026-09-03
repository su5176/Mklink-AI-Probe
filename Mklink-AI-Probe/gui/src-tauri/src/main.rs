#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

fn main() {
    #[cfg(target_os = "windows")]
    {
        let arguments = std::env::args().collect::<Vec<_>>();
        if arguments.len() == 3 && arguments[1] == "--manage-usb-port-names" {
            let code = match mklink_ai_probe_lib::run_usb_port_naming_cli(&arguments[2]) {
                Ok(_) => 0,
                Err(error) => {
                    eprintln!("MKLink USB port naming failed: {error}");
                    1
                }
            };
            std::process::exit(code);
        }
    }
    mklink_ai_probe_lib::run()
}
