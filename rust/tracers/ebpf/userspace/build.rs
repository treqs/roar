fn main() {
    let ebpf_root = std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .unwrap()
        .join("probe");

    let package = aya_build::Package {
        name: "roar-tracer-ebpf-ebpf",
        root_dir: ebpf_root.to_str().unwrap(),
        no_default_features: false,
        features: &[],
    };

    aya_build::build_ebpf([package], aya_build::Toolchain::Nightly)
        .expect("failed to build eBPF programs");
}
