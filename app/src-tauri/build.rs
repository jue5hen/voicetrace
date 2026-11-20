fn main() {
    tauri_build::build();

    // tauri-build 只把 Windows 清单塞进 bin；集成测试与库单测自己链接整个 Tauri 库，
    // 没有 Common-Controls v6 清单会在加载 comctl32 5.82 时 STATUS_ENTRYPOINT_NOT_FOUND（0xC0000139）。
    // 仅 GNU 目标需要（MSVC 的 tauri-build 一般已处理，且这里用 windres）。
    if std::env::var("CARGO_CFG_TARGET_ENV").as_deref() == Ok("gnu") {
        // [[test]] 集成测试：-tests 就够了。
        let _ = embed_resource::compile_for_tests("tests/test-resource.rc", embed_resource::NONE)
            .manifest_optional();
        // 库单测二进制（cargo test --lib 的 harness）不吃 -tests，只能用全目标 link-arg 补；
        // 仅非 release 构建补，避免给发布用 bin 再叠一份清单（.rsrc 会报 merge failure）。
        if std::env::var("PROFILE").as_deref() != Ok("release") {
            let _ = embed_resource::compile_for_everything(
                "tests/test-resource.rc",
                embed_resource::NONE,
            )
            .manifest_optional();
        }
    }
}