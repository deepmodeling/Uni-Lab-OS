//! Uni-Lab-OS 调度器的锁授予策略（FIFO over lock sets），以极小的 C ABI 导出。
//!
//! Python 侧 `unilabos/server/backend/scheduler/grant_policy.py` 是同一策略的参考实现，
//! 二者必须对任意输入给出相同结果（CI 用随机场景逐一比对）。
//!
//! 输入 / 输出都是小端 u32 序列，键与 owner 在 Python 侧映射为整数编号，不跨边界传字符串：
//!
//! ```text
//! input  := ABI_VERSION n_waiting { flags n_keys key* }* n_held key* n_reserved key*
//!           flags bit0 = 该请求是 pending handoff 的目标（不在这里授予，但阻塞后来者）
//! output := n_grants index*        （index 是 waiting 的下标，按授予顺序）
//! ```

use std::panic::{catch_unwind, AssertUnwindSafe};

/// 1.0：主版本不兼容时 Python 侧拒绝加载并退回参考实现。
pub const ABI_VERSION: u32 = 0x0001_0000;

pub const OK: i32 = 0;
pub const ERR_ARGUMENT: i32 = -1;
pub const ERR_DECODE: i32 = -2;
pub const ERR_PANIC: i32 = -3;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Waiting {
    pub keys: Vec<u32>,
    pub handoff_target: bool,
}

/// 单次扫描：按到达顺序，资源未被持有、未被 pending handoff 预留、且不与更早的等待者重叠时授予；
/// 被挡住的请求把自己的键加入阻塞集，后来者不能越过它（严格按键 FIFO，不相交的集合并行授予）。
pub fn fifo_grants(waiting: &[Waiting], held: &[u32], reserved: &[u32]) -> Vec<u32> {
    use std::collections::HashSet;
    let reserved: HashSet<u32> = reserved.iter().copied().collect();
    let mut owned: HashSet<u32> = held.iter().copied().collect();
    let mut blocked: HashSet<u32> = HashSet::new();
    let mut grants = Vec::new();
    for (index, request) in waiting.iter().enumerate() {
        if request.handoff_target {
            blocked.extend(request.keys.iter().copied());
            continue;
        }
        let conflict = request
            .keys
            .iter()
            .any(|key| reserved.contains(key) || blocked.contains(key) || owned.contains(key));
        if conflict {
            blocked.extend(request.keys.iter().copied());
            continue;
        }
        owned.extend(request.keys.iter().copied());
        grants.push(index as u32);
    }
    grants
}

struct Reader<'a> {
    words: &'a [u8],
    offset: usize,
}

impl<'a> Reader<'a> {
    fn next(&mut self) -> Option<u32> {
        let end = self.offset.checked_add(4)?;
        let bytes = self.words.get(self.offset..end)?;
        self.offset = end;
        Some(u32::from_le_bytes([bytes[0], bytes[1], bytes[2], bytes[3]]))
    }

    fn keys(&mut self) -> Option<Vec<u32>> {
        let count = self.next()? as usize;
        // 防御畸形输入：剩余字节不足以容纳声明的键数时直接判为解码失败
        if count > (self.words.len() - self.offset) / 4 {
            return None;
        }
        (0..count).map(|_| self.next()).collect()
    }
}

pub fn decode(input: &[u8]) -> Option<(Vec<Waiting>, Vec<u32>, Vec<u32>)> {
    let mut reader = Reader { words: input, offset: 0 };
    if reader.next()? >> 16 != ABI_VERSION >> 16 {
        return None;
    }
    let n_waiting = reader.next()? as usize;
    if n_waiting > input.len() / 8 {
        return None;
    }
    let mut waiting = Vec::with_capacity(n_waiting);
    for _ in 0..n_waiting {
        let flags = reader.next()?;
        let keys = reader.keys()?;
        waiting.push(Waiting { keys, handoff_target: flags & 1 == 1 });
    }
    let held = reader.keys()?;
    let reserved = reader.keys()?;
    if reader.offset != input.len() {
        return None;
    }
    Some((waiting, held, reserved))
}

pub fn encode_grants(grants: &[u32]) -> Vec<u8> {
    let mut out = Vec::with_capacity(4 * (grants.len() + 1));
    out.extend_from_slice(&(grants.len() as u32).to_le_bytes());
    for index in grants {
        out.extend_from_slice(&index.to_le_bytes());
    }
    out
}

#[no_mangle]
pub extern "C" fn uni_grant_abi_version() -> u32 {
    ABI_VERSION
}

/// # Safety
/// `input` 指向 `input_len` 个可读字节；`out_ptr` / `out_len` 可写。成功时 `*out_ptr`
/// 指向由本库分配的缓冲区，调用方必须用 `uni_grant_free` 释放。
#[no_mangle]
pub unsafe extern "C" fn uni_grant_decide(
    input: *const u8,
    input_len: usize,
    out_ptr: *mut *mut u8,
    out_len: *mut usize,
) -> i32 {
    if input.is_null() || out_ptr.is_null() || out_len.is_null() {
        return ERR_ARGUMENT;
    }
    let result = catch_unwind(AssertUnwindSafe(|| {
        let bytes = std::slice::from_raw_parts(input, input_len);
        let (waiting, held, reserved) = decode(bytes)?;
        Some(encode_grants(&fifo_grants(&waiting, &held, &reserved)))
    }));
    match result {
        Ok(Some(encoded)) => {
            let mut boxed = encoded.into_boxed_slice();
            *out_len = boxed.len();
            *out_ptr = boxed.as_mut_ptr();
            std::mem::forget(boxed);
            OK
        }
        Ok(None) => ERR_DECODE,
        Err(_) => ERR_PANIC,
    }
}

/// # Safety
/// `ptr` / `len` 必须来自 `uni_grant_decide` 的同一次成功调用，且只释放一次。
#[no_mangle]
pub unsafe extern "C" fn uni_grant_free(ptr: *mut u8, len: usize) {
    if ptr.is_null() {
        return;
    }
    drop(Box::from_raw(std::slice::from_raw_parts_mut(ptr, len)));
}

#[cfg(test)]
mod tests {
    use super::*;

    fn w(keys: &[u32]) -> Waiting {
        Waiting { keys: keys.to_vec(), handoff_target: false }
    }

    #[test]
    fn disjoint_sets_are_granted_in_parallel_and_overlap_waits() {
        let waiting = vec![w(&[1, 2]), w(&[3]), w(&[2, 4]), w(&[4])];
        // [2,4] 被 [1,2] 挡住并阻塞 4，所以 [4] 也不能越过它
        assert_eq!(fifo_grants(&waiting, &[], &[]), vec![0, 1]);
    }

    #[test]
    fn held_reserved_and_handoff_targets_block() {
        let target = Waiting { keys: vec![7], handoff_target: true };
        let waiting = vec![w(&[1]), target, w(&[7, 8]), w(&[5]), w(&[9])];
        assert_eq!(fifo_grants(&waiting, &[1], &[5]), vec![4]);
    }

    #[test]
    fn round_trip_through_the_wire_format() {
        let mut input = Vec::new();
        for word in [ABI_VERSION, 2, 0, 1, 3, 1, 1, 3, 0, 0] {
            input.extend_from_slice(&u32::to_le_bytes(word));
        }
        let (waiting, held, reserved) = decode(&input).expect("decode");
        assert_eq!(waiting.len(), 2);
        assert!(waiting[1].handoff_target);
        assert_eq!(encode_grants(&fifo_grants(&waiting, &held, &reserved)), {
            let mut out = Vec::new();
            for word in [1u32, 0] {
                out.extend_from_slice(&word.to_le_bytes());
            }
            out
        });
        assert!(decode(&input[..input.len() - 2]).is_none());
    }
}
