fn main() {
    if std::env::var("CARGO_CFG_TARGET_OS").as_deref() == Ok("macos") {
        println!("cargo:rerun-if-changed=src/interpose.c");
        cc::Build::new()
            .file("src/interpose.c")
            .warnings(false)
            .compile("roar_preload_interpose");
    }
}
