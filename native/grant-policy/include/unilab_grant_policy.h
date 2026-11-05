#ifndef UNILAB_GRANT_POLICY_H
#define UNILAB_GRANT_POLICY_H

/* 由 cbindgen 从 native/grant-policy/src/lib.rs 生成，不要手改。 */

#include <stdint.h>
#include <stddef.h>

/**
 * ABI 2.0：主版本不兼容时宿主拒绝加载并退回参考实现。
 */
#define UNI_GRANT_ABI_VERSION 131072

#define UNI_GRANT_OK 0

/**
 * 空指针、键超出 scratch 长度、输出缓冲区太小。
 */
#define UNI_GRANT_ERR_ARGUMENT -1

/**
 * 一条按到达顺序排列的等待请求。
 */
typedef struct UniGrantWaiting {
  /**
   * 该请求的全部锁键编号（必须小于 scratch_len）
   */
  const uint32_t *keys;
  size_t key_count;
  /**
   * 非 0 表示是 pending handoff 的目标：不在这里授予，但阻塞与它重叠的后来者
   */
  uint8_t handoff_target;
} UniGrantWaiting;

uint32_t uni_grant_abi_version(void);

/**
 * 计算本轮授予：授予的等待请求下标（按授予顺序）写入 `grants_out`，数量写入 `*grant_count`。
 * `scratch` 是调用方提供的 `scratch_len` 字节工作区，所有键编号必须小于 `scratch_len`；
 * `grants_capacity` 不小于 `waiting_count` 时一定放得下。
 *
 * # Safety
 * 各指针指向对应长度的有效内存；`scratch` 与 `grants_out` 可写。
 */
int32_t uni_grant_decide(const struct UniGrantWaiting *waiting,
                         size_t waiting_count,
                         const uint32_t *held,
                         size_t held_count,
                         const uint32_t *reserved,
                         size_t reserved_count,
                         uint8_t *scratch,
                         size_t scratch_len,
                         uint32_t *grants_out,
                         size_t grants_capacity,
                         size_t *grant_count);

#endif  /* UNILAB_GRANT_POLICY_H */
