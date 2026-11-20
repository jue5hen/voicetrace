//! Windows 原生能力（raw FFI，**不引入任何新 crate**）。
//!
//! 壳只有三件事必须直接调 Win32：
//! 1. `random_bytes`：CSPRNG 随机字节（`BCryptGenRandom`），绝不用时间/pid 做种子。
//! 2. `JobObject`：`JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` 作业对象，壳退出/崩溃时
//!    连引擎派生出来的 ffmpeg 孙进程一起收走。
//! 3. 单实例：命名互斥量（名字带数据目录哈希）+ 把已有窗口前置。
//!
//! 非 Windows 平台给空实现，保证 `cargo test` 与静态检查在别处也能编译。

/// 窗口标题（与 `tauri.conf.json` 的 `app.windows[0].title` 保持一致）。
pub const WINDOW_TITLE: &str = "声迹 VoiceTrace";

#[cfg(windows)]
mod plat {
    use std::ffi::{c_void, OsStr};
    use std::os::windows::ffi::OsStrExt;
    use std::path::Path;

    /// WIN32 句柄统一按 `isize` 收（HANDLE / HWND 都是指针宽度）。
    pub type Handle = isize;

    /// `ERROR_ALREADY_EXISTS`：`CreateMutexW` 之后 `GetLastError` 看到它，说明
    /// 互斥量早就被别人创建了 —— 也就是同一数据目录已经有一个实例在跑。
    const ERROR_ALREADY_EXISTS: u32 = 183;

    /// `BCRYPT_USE_SYSTEM_PREFERRED_RNG`：`hAlgorithm` 传 NULL 时由系统挑算法。
    const BCRYPT_USE_SYSTEM_PREFERRED_RNG: u32 = 0x0000_0002;

    /// `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`：最后一个作业句柄关闭时杀掉作业里所有进程。
    const JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE: u32 = 0x0000_2000;
    /// `JOBOBJECTINFOCLASS::JobObjectExtendedLimitInformation`。
    const JOB_OBJECT_EXTENDED_LIMIT_INFORMATION: i32 = 9;

    /// `PROCESS_SET_QUOTA`：`AssignProcessToJobObject` 要求的最小访问权。
    const PROCESS_SET_QUOTA: u32 = 0x0100;
    /// `PROCESS_TERMINATE`：`AssignProcessToJobObject` 要求的第二项访问权。
    const PROCESS_TERMINATE: u32 = 0x0001;

    /// `SW_RESTORE`。
    const SW_RESTORE: i32 = 9;
    /// `MB_ICONERROR`。
    const MB_ICONERROR: u32 = 0x0000_0010;

    #[allow(non_snake_case)]
    #[link(name = "kernel32")]
    extern "system" {
        // HANDLE CreateMutexW(LPSECURITY_ATTRIBUTES, BOOL, LPCWSTR);
        fn CreateMutexW(attributes: *const c_void, initial_owner: i32, name: *const u16) -> Handle;
        fn GetLastError() -> u32;
        fn CloseHandle(handle: Handle) -> i32;
        // HANDLE OpenProcess(DWORD, BOOL, DWORD);
        fn OpenProcess(desired_access: u32, inherit_handle: i32, process_id: u32) -> Handle;
        // HANDLE CreateJobObjectW(LPSECURITY_ATTRIBUTES, LPCWSTR);
        fn CreateJobObjectW(attributes: *const c_void, name: *const u16) -> Handle;
        // BOOL SetInformationJobObject(HANDLE, JOBOBJECTINFOCLASS, LPVOID, DWORD);
        fn SetInformationJobObject(
            job: Handle,
            info_class: i32,
            info: *const c_void,
            info_len: u32,
        ) -> i32;
        // BOOL AssignProcessToJobObject(HANDLE, HANDLE);
        fn AssignProcessToJobObject(job: Handle, process: Handle) -> i32;
    }

    #[allow(non_snake_case)]
    #[link(name = "bcrypt")]
    extern "system" {
        // NTSTATUS BCryptGenRandom(BCRYPT_ALG_HANDLE, PUCHAR, ULONG, ULONG);
        fn BCryptGenRandom(algorithm: *mut c_void, buffer: *mut u8, len: u32, flags: u32) -> i32;
    }

    #[allow(non_snake_case)]
    #[link(name = "user32")]
    extern "system" {
        fn FindWindowW(class_name: *const u16, window_name: *const u16) -> Handle;
        fn ShowWindow(window: Handle, cmd: i32) -> i32;
        fn SetForegroundWindow(window: Handle) -> i32;
        fn MessageBoxW(owner: Handle, text: *const u16, caption: *const u16, kind: u32) -> i32;
    }

    /// `_JOBOBJECT_BASIC_LIMIT_INFORMATION`（x64 上 64 字节）。
    #[repr(C)]
    #[derive(Default)]
    #[allow(dead_code)] // FFI 布局：字段只用于占位/写入，不在这里读。
    struct JobBasicLimitInformation {
        per_process_user_time_limit: i64,
        per_job_user_time_limit: i64,
        limit_flags: u32,
        minimum_working_set_size: usize,
        maximum_working_set_size: usize,
        active_process_limit: u32,
        affinity: usize,
        priority_class: u32,
        scheduling_class: u32,
    }

    /// `_IO_COUNTERS`（6 × ULONGLONG = 48 字节）。
    #[repr(C)]
    #[derive(Default)]
    #[allow(dead_code)] // FFI 布局：只为对齐 _IO_COUNTERS。
    struct IoCounters {
        read_operation_count: u64,
        write_operation_count: u64,
        other_operation_count: u64,
        read_transfer_count: u64,
        write_transfer_count: u64,
        other_transfer_count: u64,
    }

    /// `_JOBOBJECT_EXTENDED_LIMIT_INFORMATION`（x64 上 144 字节）。
    #[repr(C)]
    #[derive(Default)]
    #[allow(dead_code)] // FFI 布局：整体按指针传给 SetInformationJobObject。
    struct JobExtendedLimitInformation {
        basic_limit_information: JobBasicLimitInformation,
        io_info: IoCounters,
        process_memory_limit: usize,
        job_memory_limit: usize,
        peak_process_memory_used: usize,
        peak_job_memory_used: usize,
    }

    fn wide(text: &str) -> Vec<u16> {
        OsStr::new(text).encode_wide().chain(Some(0)).collect()
    }

    /// CSPRNG 随机字节：失败就返回 Err，调用方必须当作启动失败处理（不得兜底成 uuid/时间）。
    pub fn random_bytes(buffer: &mut [u8]) -> Result<(), String> {
        let len = u32::try_from(buffer.len()).map_err(|_| "随机字节缓冲区过大".to_string())?;
        let status = unsafe {
            BCryptGenRandom(
                std::ptr::null_mut(),
                buffer.as_mut_ptr(),
                len,
                BCRYPT_USE_SYSTEM_PREFERRED_RNG,
            )
        };
        if status == 0 {
            Ok(())
        } else {
            Err(format!("BCryptGenRandom 失败（NTSTATUS=0x{:08x}）", status))
        }
    }

    /// 有意泄漏句柄：作业对象一关就触发 `KILL_ON_JOB_CLOSE`，关早了会连正在跑的
    /// 引擎一起杀掉，所以只在壳退出（系统回收句柄）或显式重启时关闭。
    pub struct JobObject {
        handle: Handle,
    }

    impl JobObject {
        /// `CreateJobObjectW(NULL, NULL)` + `SetInformationJobObject(JobObjectExtendedLimitInformation)`。
        pub fn create() -> Option<Self> {
            unsafe {
                let job = CreateJobObjectW(std::ptr::null(), std::ptr::null());
                if job == 0 {
                    return None;
                }
                let mut info = JobExtendedLimitInformation::default();
                info.basic_limit_information.limit_flags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
                let ok = SetInformationJobObject(
                    job,
                    JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
                    &info as *const _ as *const c_void,
                    std::mem::size_of::<JobExtendedLimitInformation>() as u32,
                );
                if ok == 0 {
                    let _ = CloseHandle(job);
                    return None;
                }
                Some(Self { handle: job })
            }
        }

        /// `OpenProcess(PROCESS_SET_QUOTA|PROCESS_TERMINATE)` + `AssignProcessToJobObject`。
        pub fn assign_pid(&self, pid: u32) -> bool {
            unsafe {
                let process = OpenProcess(PROCESS_SET_QUOTA | PROCESS_TERMINATE, 0, pid);
                if process == 0 {
                    return false;
                }
                let ok = AssignProcessToJobObject(self.handle, process);
                // 作业对象自己持有进程引用，OpenProcess 的句柄可以立刻关。
                let _ = CloseHandle(process);
                ok != 0
            }
        }
    }

    impl Drop for JobObject {
        fn drop(&mut self) {
            if self.handle != 0 {
                unsafe {
                    let _ = CloseHandle(self.handle);
                }
                self.handle = 0;
            }
        }
    }

    /// 数据目录规范化（`canonicalize` 失败就退回原路径）+ 小写：Windows 路径不区分大小写。
    fn normalized_key(data_dir: &Path) -> String {
        let raw = std::fs::canonicalize(data_dir).unwrap_or_else(|_| data_dir.to_path_buf());
        raw.to_string_lossy().to_lowercase()
    }

    /// FNV-1a 64：只用来给名字加后缀，不承担密码学职责。
    fn fnv1a64(text: &str) -> u64 {
        let mut hash: u64 = 0xcbf2_9ce4_8422_2325;
        for byte in text.as_bytes() {
            hash ^= u64::from(*byte);
            hash = hash.wrapping_mul(0x0000_0100_0000_01b3);
        }
        hash
    }

    /// 互斥量名字：`Local\` + 固定前缀 + 数据目录哈希。不同数据目录各开一个实例。
    pub fn instance_name(data_dir: &Path) -> String {
        format!(
            "Local\\VoiceTrace.SingleInstance.{:016x}",
            fnv1a64(&normalized_key(data_dir))
        )
    }

    /// 命名互斥量探针：true = 同一数据目录已有实例。
    /// 句柄故意不关（进程结束时由系统回收），否则互斥量立刻消失，单实例就失效了。
    pub fn already_running(data_dir: &Path) -> bool {
        let name = wide(&instance_name(data_dir));
        unsafe {
            let handle = CreateMutexW(std::ptr::null(), 0, name.as_ptr());
            if handle == 0 {
                // 建不出互斥量时宁可允许启动，也不把用户挡在门外。
                return false;
            }
            GetLastError() == ERROR_ALREADY_EXISTS
        }
    }

    /// 把已有实例的窗口恢复并前置。
    pub fn focus_existing() {
        unsafe {
            let title = wide(crate::win32::WINDOW_TITLE);
            let hwnd = FindWindowW(std::ptr::null(), title.as_ptr());
            if hwnd != 0 {
                let _ = ShowWindow(hwnd, SW_RESTORE);
                let _ = SetForegroundWindow(hwnd);
            }
        }
    }

    /// 阻塞式提示框（`MessageBoxW`）。
    pub fn message(text: &str, caption: &str) {
        unsafe {
            let body = wide(text);
            let title = wide(caption);
            let _ = MessageBoxW(0, body.as_ptr(), title.as_ptr(), MB_ICONERROR);
        }
    }

    #[cfg(test)]
    mod tests {
        use super::*;

        /// x64 布局必须与 Win32 头结构逐字节一致：
        /// JOBOBJECT_BASIC_LIMIT_INFORMATION 64 + IO_COUNTERS 48 + 4×SIZE_T = 144。
        /// （字段顺序/类型对照 windows-sys 0.61.2 的 JOBOBJECT_EXTENDED_LIMIT_INFORMATION。）
        #[test]
        fn job_object_layout_matches_win32() {
            if std::mem::size_of::<usize>() != 8 {
                return; // 只为 x64 断言（项目只发 x64 GNU）
            }
            assert_eq!(std::mem::size_of::<JobBasicLimitInformation>(), 64);
            assert_eq!(std::mem::size_of::<IoCounters>(), 48);
            assert_eq!(std::mem::size_of::<JobExtendedLimitInformation>(), 144);
        }

        /// 单实例名字：同一路径大小写不同必须同名，不同数据目录必须不同名。
        #[test]
        fn instance_name_is_stable_and_data_dir_scoped() {
            let upper = instance_name(Path::new(r"D:\VoiceTrace-Test\DataA"));
            let lower = instance_name(Path::new(r"d:\voicetrace-test\dataa"));
            let other = instance_name(Path::new(r"D:\VoiceTrace-Test\DataB"));
            assert_eq!(upper, lower, "Windows 路径不区分大小写，名字必须一致");
            assert_ne!(upper, other, "不同数据目录必须能各开一个实例");
            assert!(upper.starts_with("Local\\VoiceTrace.SingleInstance."));
            assert_eq!(upper.len(), "Local\\VoiceTrace.SingleInstance.".len() + 16);
        }

        /// BCryptGenRandom 必须真的能出随机数，且两次结果不同。
        #[test]
        fn csprng_produces_bytes() {
            let (mut a, mut b) = ([0u8; 16], [0u8; 16]);
            random_bytes(&mut a).expect("BCryptGenRandom 应当可用");
            random_bytes(&mut b).expect("BCryptGenRandom 应当可用");
            assert_ne!(a, b);
            assert_ne!(a, [0u8; 16]);
        }
    }
}


#[cfg(not(windows))]
mod plat {
    use std::io::Read;
    use std::path::Path;

    /// 非 Windows 用 `/dev/urandom`（同样不是时间种子）。
    pub fn random_bytes(buffer: &mut [u8]) -> Result<(), String> {
        let mut file =
            std::fs::File::open("/dev/urandom").map_err(|e| format!("/dev/urandom 打不开：{e}"))?;
        file.read_exact(buffer)
            .map_err(|e| format!("读取 /dev/urandom 失败：{e}"))
    }

    /// 非 Windows 没有作业对象，调用方会退回 `kill_tree`。
    pub struct JobObject;

    impl JobObject {
        pub fn create() -> Option<Self> {
            None
        }

        pub fn assign_pid(&self, _pid: u32) -> bool {
            false
        }
    }

    pub fn instance_name(_data_dir: &Path) -> String {
        "Local\\VoiceTrace.SingleInstance".to_string()
    }

    pub fn already_running(_data_dir: &Path) -> bool {
        false
    }

    pub fn focus_existing() {}

    pub fn message(text: &str, _caption: &str) {
        eprintln!("{text}");
    }
}

pub use plat::*;
