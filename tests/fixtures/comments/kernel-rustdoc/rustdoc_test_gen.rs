// источник: Linux 7.2.9, scripts/rustdoc_test_gen.rs, строки 40–49 и 160–240
/// `rustdoc` generated `file`s look like `sync_locked_by_rs`. Underscores (except the last one)
/// may represent an actual underscore in a directory/file, or a path separator. Thus the actual
/// file might be `sync_locked_by.rs`, `sync/locked_by.rs`, `sync_locked/by.rs` or
/// `sync/locked/by.rs`. This function walks the file system to determine which is the real one.
///
/// This does require that ambiguities do not exist, but that seems fair, especially since this is
/// all supposed to be temporary until `rustdoc` gives us proper metadata to build this. If such
/// ambiguities are detected, they are diagnosed and the script panics.
fn find_real_path<'a>(srctree: &Path, valid_paths: &'a mut Vec<PathBuf>, file: &str) -> &'a str {
    valid_paths.clear();
        let body_offset = body
            .lines()
            .take_while(|line| !line.contains("fn main() {"))
            .count()
            + 1;

        use std::fmt::Write;
        write!(
            rust_tests,
            r#"/// Generated `{name}` KUnit test case from a Rust documentation test.
#[no_mangle]
pub extern "C" fn {kunit_name}(__kunit_test: *mut ::kernel::bindings::kunit) {{
    /// Overrides the usual [`assert!`] macro with one that calls KUnit instead.
    #[allow(unused)]
    macro_rules! assert {{
        ($cond:expr $(,)?) => {{{{
            ::kernel::kunit_assert!(
                "{kunit_name}", c"{real_path}", __DOCTEST_ANCHOR - {line}, $cond
            );
        }}}}
    }}

    /// Overrides the usual [`assert_eq!`] macro with one that calls KUnit instead.
    #[allow(unused)]
    macro_rules! assert_eq {{
        ($left:expr, $right:expr $(,)?) => {{{{
            ::kernel::kunit_assert_eq!(
                "{kunit_name}", c"{real_path}", __DOCTEST_ANCHOR - {line}, $left, $right
            );
        }}}}
    }}

    // Many tests need the prelude, so provide it by default.
    #[allow(unused)]
    use ::kernel::prelude::*;

    // Unconditionally print the location of the original doctest (i.e. rather than the location in
    // the generated file) so that developers can easily map the test back to the source code.
    //
    // This information is also printed when assertions fail, but this helps in the successful cases
    // when the user is running KUnit manually, or when passing `--raw_output` to `kunit.py`.
    //
    // This follows the syntax for declaring test metadata in the proposed KTAP v2 spec, which may
    // be used for the proposed KUnit test attributes API. Thus hopefully this will make migration
    // easier later on.
    ::kernel::kunit::info(fmt!("    # {kunit_name}.location: {real_path}:{line}\n"));

    /// The anchor where the test code body starts.
    #[allow(unused)]
    static __DOCTEST_ANCHOR: i32 = ::core::line!() as i32 + {body_offset} + 2;
    {{
        #![allow(unreachable_pub, clippy::disallowed_names)]
        {body}
        main();
    }}
}}

"#
        )
        .unwrap();

        write!(c_test_declarations, "void {kunit_name}(struct kunit *);\n").unwrap();
        write!(c_test_cases, "    KUNIT_CASE({kunit_name}),\n").unwrap();
    }

    let rust_tests = rust_tests.trim();
    let c_test_declarations = c_test_declarations.trim();
    let c_test_cases = c_test_cases.trim();

    write!(
        BufWriter::new(File::create("rust/doctests_kernel_generated.rs").unwrap()),
        r#"//! `kernel` crate documentation tests.

const __LOG_PREFIX: &[u8] = b"rust_doctests_kernel\0";

{rust_tests}
"#
    )
    .unwrap();

    write!(
