import React, { useEffect, useRef } from 'react';

type Props = {
  markdown: string;
  busy: boolean;
  onClose: () => void;
};

export function OpcProfileSpecDialog({ markdown, busy, onClose }: Props) {
  const dialogRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    const dialog = dialogRef.current;
    dialog?.focus();
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.preventDefault();
        onClose();
      }
    };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, [onClose]);

  return (
    <div className="opc-profile-backdrop" onMouseDown={onClose}>
      <div
        aria-labelledby="opc-profile-spec-title"
        aria-modal="true"
        className="opc-profile-dialog opc-profile-spec-dialog"
        onMouseDown={(event) => event.stopPropagation()}
        ref={dialogRef}
        role="dialog"
        tabIndex={-1}
      >
        <header className="opc-profile-header">
          <div>
            <p>OPC SIMULATOR / PROFILE V2</p>
            <h2 id="opc-profile-spec-title">JSON 生成规范</h2>
            <small className="opc-profile-subtitle">
              供大模型或人工编写 schema v2 配置；生成后写入 task-orchestration/configs，在下拉框选择并打开配置工作台。
            </small>
          </div>
          <button aria-label="关闭生成规范" className="opc-close" onClick={onClose} type="button">×</button>
        </header>
        <div className="opc-profile-spec-body">
          <pre>{markdown}</pre>
        </div>
        <footer className="opc-profile-footer">
          <div>
            <span>REFERENCE SPEC</span>
            <small>docs/developer_guide/opc_simulator_profile_v2.md</small>
          </div>
          <button
            className="opc-command"
            disabled={busy}
            onClick={async () => {
              try {
                await navigator.clipboard.writeText(markdown);
              } catch {
                // 复制失败时仍保留对话框，用户可手动选择文本
              }
            }}
            type="button"
          >复制规范</button>
          <button className="opc-command" disabled={busy} onClick={onClose} type="button">关闭</button>
        </footer>
      </div>
    </div>
  );
}
