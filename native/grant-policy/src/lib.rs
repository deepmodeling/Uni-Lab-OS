//! Uni-Lab-OS 调度器的锁授予策略（按键严格 FIFO），以极小的 C ABI 导出。
//!
//! `no_std`、不分配内存：键与等待请求在宿主侧编号为整数，键状态用调用方给的 scratch 缓冲区
//! （每个键一个字节，长度即键空间），结果写入调用方给的输出缓冲区。所有访问都做边界检查并以
//! 错误码返回，没有会 panic 的路径。C 头文件由 build.rs 经 cbindgen 生成，Python 绑定再由头文件
//! 生成。Python 参考实现 `unilabos/server/backend/scheduler/grant/policy.py::fifo_grants` 必须与
//! 这里逐条等价。
#![cfg_attr(not(test), no_std)]

/// ABI 2.0：主版本不兼容时宿主拒绝加载并退回参考实现。
pub const UNI_GRANT_ABI_VERSION: u32 = 0x0002_0000;
pub const UNI_GRANT_OK: i32 = 0;
/// 空指针、键超出 scratch 长度、输出缓冲区太小。
pub const UNI_GRANT_ERR_ARGUMENT: i32 = -1;

/// 一条按到达顺序排列的等待请求。
#[repr(C)]
pub struct UniGrantWaiting {
    /// 该请求的全部锁键编号（必须小于 scratch_len）
    pub keys: *const u32,
    pub key_count: usize,
    /// 非 0 表示是 pending handoff 的目标：不在这里授予，但阻塞与它重叠的后来者
    pub handoff_target: u8,
}

const OWNED: u8 = 1;
const BLOCKED: u8 = 2;
const RESERVED: u8 = 4;

unsafe fn view<'a, T>(ptr: *const T, len: usize) -> Option<&'a [T]> {
    if len == 0 {
        Some(&[])
    } else if ptr.is_null() {
        None
    } else {
        Some(core::slice::from_raw_parts(ptr, len))
    }
}

unsafe fn view_mut<'a, T>(ptr: *mut T, len: usize) -> Option<&'a mut [T]> {
    if len == 0 {
        Some(&mut [])
    } else if ptr.is_null() {
        None
    } else {
        Some(core::slice::from_raw_parts_mut(ptr, len))
    }
}

/// 单次扫描：资源未被持有、未被预留、且不与更早的等待者重叠时授予；被挡住的请求把自己的键
/// 标为阻塞，后来者不能越过它。不相交的集合并行授予。返回授予数量。
unsafe fn decide(
    waiting: &[UniGrantWaiting],
    held: &[u32],
    reserved: &[u32],
    state: &mut [u8],
    grants: &mut [u32],
) -> Option<usize> {
    for slot in state.iter_mut() {
        *slot = 0;
    }
    for &key in held {
        *state.get_mut(key as usize)? |= OWNED;
    }
    for &key in reserved {
        *state.get_mut(key as usize)? |= RESERVED;
    }
    let mut count = 0usize;
    for (index, request) in waiting.iter().enumerate() {
        let keys = view(request.keys, request.key_count)?;
        let mut conflict = request.handoff_target != 0;
        for &key in keys {
            conflict |= *state.get(key as usize)? != 0;
        }
        let mark = if conflict { BLOCKED } else { OWNED };
        for &key in keys {
            *state.get_mut(key as usize)? |= mark;
        }
        if !conflict {
            *grants.get_mut(count)? = index as u32;
            count += 1;
        }
    }
    Some(count)
}

#[no_mangle]
pub extern "C" fn uni_grant_abi_version() -> u32 {
    UNI_GRANT_ABI_VERSION
}

/// 计算本轮授予：授予的等待请求下标（按授予顺序）写入 `grants_out`，数量写入 `*grant_count`。
/// `scratch` 是调用方提供的 `scratch_len` 字节工作区，所有键编号必须小于 `scratch_len`；
/// `grants_capacity` 不小于 `waiting_count` 时一定放得下。
///
/// # Safety
/// 各指针指向对应长度的有效内存；`scratch` 与 `grants_out` 可写。
#[no_mangle]
pub unsafe extern "C" fn uni_grant_decide(
    waiting: *const UniGrantWaiting,
    waiting_count: usize,
    held: *const u32,
    held_count: usize,
    reserved: *const u32,
    reserved_count: usize,
    scratch: *mut u8,
    scratch_len: usize,
    grants_out: *mut u32,
    grants_capacity: usize,
    grant_count: *mut usize,
) -> i32 {
    if grant_count.is_null() {
        return UNI_GRANT_ERR_ARGUMENT;
    }
    let result = (|| {
        decide(
            view(waiting, waiting_count)?,
            view(held, held_count)?,
            view(reserved, reserved_count)?,
            view_mut(scratch, scratch_len)?,
            view_mut(grants_out, grants_capacity)?,
        )
    })();
    match result {
        Some(count) => {
            *grant_count = count;
            UNI_GRANT_OK
        }
        None => UNI_GRANT_ERR_ARGUMENT,
    }
}

#[cfg(not(test))]
#[panic_handler]
fn panic(_: &core::panic::PanicInfo) -> ! {
    // 上面没有会 panic 的路径；万一出现，终止而不是越界写内存
    extern "C" {
        fn abort() -> !;
    }
    unsafe { abort() }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn run(waiting: &[(&[u32], bool)], held: &[u32], reserved: &[u32], key_space: usize) -> Result<Vec<u32>, i32> {
        let requests: Vec<UniGrantWaiting> = waiting
            .iter()
            .map(|(keys, target)| UniGrantWaiting { keys: keys.as_ptr(), key_count: keys.len(), handoff_target: *target as u8 })
            .collect();
        let mut scratch = vec![0u8; key_space];
        let mut out = vec![0u32; requests.len()];
        let mut count = 0usize;
        let status = unsafe {
            uni_grant_decide(
                requests.as_ptr(), requests.len(), held.as_ptr(), held.len(), reserved.as_ptr(), reserved.len(),
                scratch.as_mut_ptr(), scratch.len(), out.as_mut_ptr(), out.len(), &mut count,
            )
        };
        if status == UNI_GRANT_OK {
            out.truncate(count);
            Ok(out)
        } else {
            Err(status)
        }
    }

    #[test]
    fn disjoint_sets_are_granted_in_parallel_and_overlap_waits() {
        assert_eq!(run(&[(&[1, 2], false), (&[3], false), (&[2, 4], false), (&[4], false)], &[], &[], 5), Ok(vec![0, 1]));
    }

    #[test]
    fn held_reserved_and_handoff_targets_block() {
        let waiting: &[(&[u32], bool)] = &[(&[1], false), (&[7], true), (&[7, 8], false), (&[5], false), (&[9], false)];
        assert_eq!(run(waiting, &[1], &[5], 10), Ok(vec![4]));
    }

    #[test]
    fn invalid_arguments_are_errors_not_panics() {
        assert_eq!(run(&[(&[3], false)], &[], &[], 3), Err(UNI_GRANT_ERR_ARGUMENT));
        assert_eq!(run(&[], &[9], &[], 3), Err(UNI_GRANT_ERR_ARGUMENT));
        let mut count = 0usize;
        let waiting = [UniGrantWaiting { keys: [0u32].as_ptr(), key_count: 1, handoff_target: 0 }];
        let mut scratch = [0u8; 1];
        let too_small = unsafe {
            uni_grant_decide(waiting.as_ptr(), 1, core::ptr::null(), 0, core::ptr::null(), 0, scratch.as_mut_ptr(), 1, core::ptr::null_mut(), 0, &mut count)
        };
        assert_eq!(too_small, UNI_GRANT_ERR_ARGUMENT);
    }
}
