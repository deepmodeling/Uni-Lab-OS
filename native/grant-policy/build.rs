//! 每次编译都从 `src/lib.rs` 的 C ABI 声明重新生成 `include/unilab_grant_policy.h`。
//! Python 侧的 ctypes 绑定再由 `scripts/gen_grant_bindings.py` 从这份头文件生成，
//! 两份生成物都提交在仓库里，CI 重新生成后 `git diff --exit-code` 检查是否过期。

use std::env;
use std::path::PathBuf;

fn main() {
    let crate_dir = PathBuf::from(env::var("CARGO_MANIFEST_DIR").expect("CARGO_MANIFEST_DIR"));
    let header = crate_dir.join("include").join("unilab_grant_policy.h");
    let config = cbindgen::Config::from_file(crate_dir.join("cbindgen.toml")).expect("cbindgen.toml");
    cbindgen::Builder::new()
        .with_crate(&crate_dir)
        .with_config(config)
        .generate()
        .expect("生成 C 头文件失败")
        .write_to_file(&header);
    println!("cargo:rerun-if-changed=src/lib.rs");
    println!("cargo:rerun-if-changed=cbindgen.toml");
}
