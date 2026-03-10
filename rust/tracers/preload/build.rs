fn main() {
    println!("cargo:rerun-if-changed=src/interpose.c");
    cc::Build::new()
        .file("src/interpose.c")
        .warnings(false)
        .compile("roar_preload_interpose");
}
