fn main() {
    println!("cargo:rerun-if-changed=build.rs");
    println!("cargo:rerun-if-changed=tauri.conf.json");
    println!("cargo:rerun-if-changed=tauri.release.conf.json");
    println!("cargo:rerun-if-changed=tauri.unsigned.conf.json");
    println!("cargo:rerun-if-changed=capabilities/default.json");
    tauri_build::build()
}
