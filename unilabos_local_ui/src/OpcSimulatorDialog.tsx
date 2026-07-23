import React, { useEffect, useMemo, useRef, useState } from 'react';
import {
  addProfileCondition,
  addProfileVariable,
  addProfileWrite,
  clearProfileVariableInitialValue,
  countNodeMissingFields,
  formatJsonScalar,
  parseJsonScalar,
  removeProfileCondition,
  removeProfileVariable,
  removeProfileWrite,
  updateProfileCondition,
  updateProfileVariable,
  updateProfileWrite,
  validationPathsToErrors,
  type JsonScalar,
  type OpcProfileValidationError,
  type OpcSimulatorCondition,
  type OpcSimulatorConditionGroup,
  type OpcSimulatorDelayedPhase,
  type OpcSimulatorPhase,
  type OpcSimulatorProfile,
  type OpcSimulatorVariable,
} from './opcSimulatorProfile';

type Props = {
  profile: OpcSimulatorProfile;
  fileName: string;
  profileFiles?: string[];
  configDir?: string;
  errors: OpcProfileValidationError[];
  backendErrors: string[];
  busy: boolean;
  revision: string | null;
  dirty: boolean;
  readOnly?: boolean;
  subtitle?: string;
  onProfileChange: (profile: OpcSimulatorProfile) => void;
  onConfigFileChange?: (fileName: string) => void;
  onClose: () => void;
  onSave: (status: 'draft' | 'runnable', download: boolean) => void;
  onReload: () => void;
};

type GroupField = 'trigger' | 'reset_when';
type PhaseField = 'on_trigger' | 'on_complete' | 'after_reset';

function focusableElements(container: HTMLElement) {
  return Array.from(container.querySelectorAll<HTMLElement>(
    'button:not([disabled]), input:not([disabled]), select:not([disabled]), '
    + 'textarea:not([disabled]), a[href], [tabindex]:not([tabindex="-1"])',
  )).filter((element) => (
    element.isConnected
    && element.getAttribute('aria-hidden') !== 'true'
    && element.getClientRects().length > 0
  ));
}

function ScalarInput({
  label,
  value,
  onChange,
}: {
  label: string;
  value: JsonScalar;
  onChange: (value: JsonScalar) => void;
}) {
  const [text, setText] = useState(() => formatJsonScalar(value));
  useEffect(() => setText(formatJsonScalar(value)), [value]);
  return (
    <input
      aria-label={label}
      className="opc-scalar-input"
      value={text}
      onBlur={() => {
        try {
          onChange(parseJsonScalar(text));
        } catch {
          setText(formatJsonScalar(value));
        }
      }}
      onChange={(event) => setText(event.target.value)}
    />
  );
}

function VariableSelect({
  label,
  value,
  variables,
  writableOnly = false,
  onChange,
}: {
  label: string;
  value: string;
  variables: OpcSimulatorVariable[];
  writableOnly?: boolean;
  onChange: (value: string) => void;
}) {
  const choices = useMemo(() => variables
    .filter((variable) => !writableOnly || variable.direction === 'plc_to_pc')
    .sort((left, right) => {
      const priority = (source: string) => (
        source === 'task_input' || source === 'input'
          ? 0
          : source === 'task_output' || source === 'output'
            ? 1
            : 2
      );
      return priority(left.source) - priority(right.source);
    }), [variables, writableOnly]);
  return (
    <select aria-label={label} value={value} onChange={(event) => onChange(event.target.value)}>
      <option value="">选择真实变量…</option>
      {choices.map((variable) => (
        <option key={variable.name} value={variable.name}>
          {variable.source} · {variable.name}
        </option>
      ))}
    </select>
  );
}

export function OpcSimulatorDialog({
  profile,
  fileName,
  profileFiles,
  configDir,
  errors,
  backendErrors,
  busy,
  revision,
  dirty,
  readOnly = false,
  subtitle,
  onProfileChange,
  onConfigFileChange,
  onClose,
  onSave,
  onReload,
}: Props) {
  const editLocked = busy || readOnly;
  const dialogRef = useRef<HTMLDivElement | null>(null);
  const previousActiveElementRef = useRef<HTMLElement | null>(null);
  const onCloseRef = useRef(onClose);
  const [selectedNodeIndex, setSelectedNodeIndex] = useState(0);
  const selectedNode = profile.nodes[selectedNodeIndex] || null;
  const backendValidationErrors = useMemo(
    () => validationPathsToErrors(backendErrors, profile),
    [backendErrors, profile],
  );
  const validationIssues = useMemo(
    () => [...errors, ...backendValidationErrors],
    [errors, backendValidationErrors],
  );

  useEffect(() => {
    onCloseRef.current = onClose;
  }, [onClose]);

  useEffect(() => {
    const previousActiveElement = document.activeElement instanceof HTMLElement
      ? document.activeElement
      : null;
    previousActiveElementRef.current = previousActiveElement;
    const dialog = dialogRef.current;
    const focusables = dialog ? focusableElements(dialog) : [];
    focusables[0]?.focus();
    if (!focusables.length) dialogRef.current?.focus();
    const keepFocusInDialog = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.preventDefault();
        onCloseRef.current();
        return;
      }
      if (event.key !== 'Tab' || !dialogRef.current) return;
      const currentDialog = dialogRef.current;
      const currentFocusables = focusableElements(currentDialog);
      if (!currentFocusables.length) {
        event.preventDefault();
        currentDialog.focus();
        return;
      }
      const first = currentFocusables[0];
      const last = currentFocusables[currentFocusables.length - 1];
      const active = document.activeElement;
      if (event.shiftKey && (active === first || !currentDialog.contains(active))) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && (active === last || !currentDialog.contains(active))) {
        event.preventDefault();
        first.focus();
      }
    };
    document.addEventListener('keydown', keepFocusInDialog);
    return () => {
      document.removeEventListener('keydown', keepFocusInDialog);
      if (previousActiveElement?.isConnected) previousActiveElement.focus();
      previousActiveElementRef.current = null;
    };
  }, []);

  useEffect(() => {
    if (selectedNodeIndex >= profile.nodes.length) setSelectedNodeIndex(Math.max(0, profile.nodes.length - 1));
  }, [profile.nodes.length, selectedNodeIndex]);

  const updateNode = (patch: Partial<NonNullable<typeof selectedNode>>) => {
    if (!selectedNode) return;
    onProfileChange({
      ...profile,
      nodes: profile.nodes.map((node, index) => index === selectedNodeIndex ? { ...node, ...patch } : node),
    });
  };
  const defaultScalar = (variableName: string): JsonScalar => {
    const variable = profile.variables.find((item) => item.name === variableName);
    if (variable?.data_type === 'bool') return false;
    if (variable?.data_type === 'int' || variable?.data_type === 'float') return 0;
    return '';
  };
  const addCondition = (field: GroupField) => {
    const variable = profile.variables.find((item) => item.source === 'task_input') || profile.variables[0];
    const condition: OpcSimulatorCondition = {
      variable: variable?.name || '',
      operator: 'eq',
      edge: 'level',
      value: defaultScalar(variable?.name || ''),
    };
    const next = field === 'reset_when' && !selectedNode?.reset_when
      ? {
          ...profile,
          nodes: profile.nodes.map((node, index) => index === selectedNodeIndex
            ? { ...node, reset_when: { all: [condition] }, after_reset: { delay: 0, writes: [] } }
            : node),
        }
      : addProfileCondition(profile, selectedNodeIndex, field, condition);
    onProfileChange(next);
  };
  const addWrite = (field: PhaseField) => {
    const variable = profile.variables.find((item) => item.direction === 'plc_to_pc');
    onProfileChange(addProfileWrite(profile, selectedNodeIndex, field, {
      variable: variable?.name || '',
      value: defaultScalar(variable?.name || ''),
    }));
  };
  const renderConditions = (field: GroupField, group: OpcSimulatorConditionGroup | null) => (
    <div className="opc-structured-list">
      {(group?.all || []).map((condition, index) => (
        <div className="opc-condition-row" key={`${field}-${index}`}>
          <VariableSelect
            label={`${field} 条件变量 ${index + 1}`}
            value={condition.variable}
            variables={profile.variables}
            onChange={(variable) => onProfileChange(updateProfileCondition(
              profile,
              selectedNodeIndex,
              field,
              index,
              { variable, value: defaultScalar(variable) },
            ))}
          />
          <select
            aria-label={`${field} operator ${index + 1}`}
            value={condition.operator}
            onChange={() => undefined}
          ><option value="eq">eq</option></select>
          <select
            aria-label={`${field} edge ${index + 1}`}
            value={condition.edge}
            onChange={(event) => onProfileChange(updateProfileCondition(
              profile,
              selectedNodeIndex,
              field,
              index,
              { edge: event.target.value as OpcSimulatorCondition['edge'] },
            ))}
          >
            <option value="level">level</option>
            <option value="rising">rising</option>
            <option value="falling">falling</option>
          </select>
          <ScalarInput
            label={`${field} 条件值 ${index + 1}`}
            value={condition.value}
            onChange={(value) => onProfileChange(updateProfileCondition(profile, selectedNodeIndex, field, index, { value }))}
          />
          <button
            aria-label={`删除 ${field} 条件 ${index + 1}`}
            className="opc-mini-danger"
            onClick={() => onProfileChange(removeProfileCondition(profile, selectedNodeIndex, field, index))}
            type="button"
          >×</button>
        </div>
      ))}
      <button className="opc-add-row" onClick={() => addCondition(field)} type="button">＋ 添加条件</button>
    </div>
  );

  const renderPhase = (
    field: PhaseField,
    value: OpcSimulatorPhase | OpcSimulatorDelayedPhase | null,
  ) => (
    <div className="opc-structured-list">
      {field !== 'on_trigger' && value && 'delay' in value && (
        <label className="opc-inline-field">
          delay / s
          <input
            aria-label={`${field} delay`}
            min={0}
            step="any"
            type="number"
            value={value.delay}
            onChange={(event) => updateNode({ [field]: { ...value, delay: Number(event.target.value) } })}
          />
        </label>
      )}
      {(value?.writes || []).map((write, index) => (
        <div className="opc-write-row" key={`${field}-${index}`}>
          <VariableSelect
            label={`${field} 写变量 ${index + 1}`}
            value={write.variable}
            variables={profile.variables}
            writableOnly
            onChange={(variable) => onProfileChange(updateProfileWrite(
              profile,
              selectedNodeIndex,
              field,
              index,
              { variable, value: defaultScalar(variable) },
            ))}
          />
          <ScalarInput
            label={`${field} 写值 ${index + 1}`}
            value={write.value}
            onChange={(scalar) => onProfileChange(updateProfileWrite(
              profile,
              selectedNodeIndex,
              field,
              index,
              { value: scalar },
            ))}
          />
          <button
            aria-label={`删除 ${field} 写值 ${index + 1}`}
            className="opc-mini-danger"
            onClick={() => onProfileChange(removeProfileWrite(profile, selectedNodeIndex, field, index))}
            type="button"
          >×</button>
        </div>
      ))}
      <button className="opc-add-row" onClick={() => addWrite(field)} type="button">＋ 添加写值</button>
    </div>
  );

  return (
    <div className="opc-profile-backdrop" onMouseDown={onClose}>
      <div
        aria-labelledby="opc-profile-title"
        aria-modal="true"
        className="opc-profile-dialog"
        onMouseDown={(event) => event.stopPropagation()}
        ref={dialogRef}
        role="dialog"
        tabIndex={-1}
      >
        <header className="opc-profile-header">
          <div>
            <p>OPC SIMULATOR / PROFILE V2</p>
            <h2 id="opc-profile-title">{readOnly ? '配置模板（只读参照）' : '模拟配置控制台'}</h2>
            {subtitle ? <small className="opc-profile-subtitle">{subtitle}</small> : null}
          </div>
          <fieldset className="opc-profile-meta opc-profile-edit-lock" disabled={editLocked}>
            {readOnly ? (
              <label>参考文件<input aria-label="参考文件" readOnly value={fileName} /></label>
            ) : (
              <label className="opc-config-file-field">
                配置文件
                <select
                  aria-label="配置文件"
                  disabled={!profileFiles?.length && !fileName}
                  onChange={(event) => onConfigFileChange?.(event.target.value)}
                  value={fileName}
                >
                  {fileName && !profileFiles?.includes(fileName) && (
                    <option value={fileName}>{fileName}（未保存）</option>
                  )}
                  {(profileFiles || []).map((file) => (
                    <option key={file} value={file}>{file}</option>
                  ))}
                  {!profileFiles?.length && !fileName && (
                    <option value="">task-orchestration/configs 暂无配置</option>
                  )}
                </select>
                {configDir ? <small className="opc-config-dir-hint">{configDir}</small> : null}
              </label>
            )}
            <label>OPC URL<input aria-label="模拟器 OPC URL" value={profile.opc.url} onChange={(event) => onProfileChange({ ...profile, opc: { ...profile.opc, url: event.target.value } })} /></label>
            <label>Poll interval
              <input
                aria-label="OPC 轮询间隔"
                max={60}
                min={0.05}
                step="any"
                type="number"
                value={profile.opc.poll_interval}
                onChange={(event) => onProfileChange({
                  ...profile,
                  opc: { ...profile.opc, poll_interval: Number(event.target.value) },
                })}
              />
            </label>
            <label>I/O timeout
              <input
                aria-label="OPC I/O 超时"
                max={60}
                min={0.1}
                step="any"
                type="number"
                value={profile.opc.io_timeout}
                onChange={(event) => onProfileChange({
                  ...profile,
                  opc: { ...profile.opc, io_timeout: Number(event.target.value) },
                })}
              />
            </label>
          </fieldset>
          <button aria-label="关闭 OPC 模拟配置" className="opc-close" onClick={onClose} type="button">×</button>
        </header>

        <div className={`opc-profile-grid${readOnly ? ' opc-profile-readonly' : ''}`}>
          <aside className="opc-profile-nodes">
            <div className="opc-column-heading"><strong>节点索引</strong><span>{profile.nodes.length}</span></div>
            {profile.nodes.map((node, index) => {
              const missing = countNodeMissingFields(node, profile.variables);
              return (
                <button
                  className={index === selectedNodeIndex ? 'active' : ''}
                  key={node.workflow_node_id}
                  onClick={() => setSelectedNodeIndex(index)}
                  type="button"
                >
                  <span>{String(index + 1).padStart(2, '0')}</span>
                  <strong>{node.method || node.workflow_node_id}</strong>
                  <small>{node.device_id || '未指定设备'}</small>
                  <em className={missing ? 'missing' : 'ready'}>{missing ? `${missing} 缺失` : 'READY'}</em>
                </button>
              );
            })}
          </aside>

          <fieldset className="opc-profile-editor-shell opc-profile-edit-lock" disabled={editLocked}>
          <main className="opc-profile-editor">
            <section className="opc-editor-section">
              <div className="opc-column-heading"><strong>变量目录</strong><span>{profile.variables.length}</span></div>
              <div className="opc-variable-table">
                {profile.variables.map((variable, index) => (
                  <div className="opc-variable-row" key={`${variable.name}-${index}`}>
                    <input aria-label={`变量名 ${index + 1}`} value={variable.name} onChange={(event) => onProfileChange(updateProfileVariable(profile, index, { name: event.target.value }))} />
                    <select
                      aria-label={`变量方向 ${index + 1}`}
                      value={variable.direction}
                      onChange={(event) => {
                        const direction = event.target.value as OpcSimulatorVariable['direction'];
                        const withDirection = updateProfileVariable(profile, index, { direction });
                        onProfileChange(
                          direction === 'plc_to_pc'
                            ? withDirection
                            : clearProfileVariableInitialValue(withDirection, index),
                        );
                      }}
                    >
                      <option value="unknown">unknown</option>
                      <option value="pc_to_plc">PC → PLC</option>
                      <option value="plc_to_pc">PLC → PC</option>
                    </select>
                    <select aria-label={`变量类型 ${index + 1}`} value={variable.data_type} onChange={(event) => onProfileChange(updateProfileVariable(profile, index, { data_type: event.target.value as OpcSimulatorVariable['data_type'] }))}>
                      <option value="unknown">unknown</option>
                      <option value="bool">bool</option>
                      <option value="int">int</option>
                      <option value="float">float</option>
                      <option value="string">string</option>
                    </select>
                    <div className="opc-initial-cell">
                      {variable.direction === 'plc_to_pc' && variable.initial_value !== undefined ? (
                        <>
                          <ScalarInput
                            label={`变量初始值 ${index + 1}`}
                            value={variable.initial_value}
                            onChange={(initial_value) => onProfileChange(updateProfileVariable(profile, index, { initial_value }))}
                          />
                          <button
                            aria-label={`清除变量初始值 ${index + 1}`}
                            className="opc-mini-danger"
                            onClick={() => onProfileChange(clearProfileVariableInitialValue(profile, index))}
                            type="button"
                          >×</button>
                        </>
                      ) : variable.direction === 'plc_to_pc' ? (
                        <button
                          className="opc-add-row"
                          onClick={() => onProfileChange(updateProfileVariable(profile, index, {
                            initial_value: defaultScalar(variable.name),
                          }))}
                          type="button"
                        >设初值</button>
                      ) : <small>READ ONLY</small>}
                    </div>
                    <code>{variable.source}</code>
                    {!readOnly && (
                      <button aria-label={`删除变量 ${variable.name || index + 1}`} className="opc-mini-danger" onClick={() => onProfileChange(removeProfileVariable(profile, index))} type="button">×</button>
                    )}
                  </div>
                ))}
                {!readOnly && (
                  <button
                    className="opc-add-row"
                    onClick={() => onProfileChange(addProfileVariable(profile, {
                      name: `manual_variable_${profile.variables.length + 1}`,
                      direction: 'unknown',
                      data_type: 'bool',
                      source: 'manual',
                    }))}
                    type="button"
                  >＋ 手工添加真实变量</button>
                )}
              </div>
            </section>

            {selectedNode && (
              <>
                <section className="opc-editor-section">
                  <div className="opc-column-heading"><strong>当前节点</strong><span>{selectedNode.workflow_node_id}</span></div>
                  <div className="opc-node-summary">
                    <label>设备<input readOnly value={selectedNode.device_id} /></label>
                    <label>方法<input readOnly value={selectedNode.method} /></label>
                    <label>Task IDs<input readOnly value={selectedNode.task_template_ids.join(', ')} /></label>
                    <label>参数<textarea readOnly value={JSON.stringify(selectedNode.params)} /></label>
                    <label>Channel<input aria-label="节点 channel" value={selectedNode.channel} onChange={(event) => updateNode({ channel: event.target.value })} /></label>
                  </div>
                </section>
                <section className="opc-editor-section"><h3>Trigger · ALL CONDITIONS</h3>{renderConditions('trigger', selectedNode.trigger)}</section>
                <section className="opc-editor-section"><h3>On trigger · WRITES</h3>{renderPhase('on_trigger', selectedNode.on_trigger)}</section>
                <section className="opc-editor-section"><h3>On complete · DELAY / WRITES</h3>{renderPhase('on_complete', selectedNode.on_complete)}</section>
                <section className="opc-editor-section">
                  <h3>Reset when · OPTIONAL</h3>
                  {selectedNode.reset_when
                    ? <>
                        {renderConditions('reset_when', selectedNode.reset_when)}
                        <button className="opc-add-row" onClick={() => updateNode({ reset_when: null, after_reset: null })} type="button">移除复位阶段</button>
                      </>
                    : <button className="opc-add-row" onClick={() => addCondition('reset_when')} type="button">＋ 启用 reset_when</button>}
                </section>
                {selectedNode.after_reset && <section className="opc-editor-section"><h3>After reset · DELAY / WRITES</h3>{renderPhase('after_reset', selectedNode.after_reset)}</section>}
              </>
            )}
          </main>

          <aside className="opc-profile-validation">
            <div className="opc-column-heading">
              <strong>校验问题</strong>
              <span>{validationIssues.length}</span>
            </div>
            <div className="opc-profile-errors" aria-live="polite">
              {validationIssues.map((error) => (
                <button
                  key={`${error.path}-${error.message}`}
                  onClick={() => {
                    if ('nodeId' in error && error.nodeId) {
                      const index = profile.nodes.findIndex((node) => node.workflow_node_id === error.nodeId);
                      if (index >= 0) setSelectedNodeIndex(index);
                    }
                  }}
                  type="button"
                ><code>{error.path}</code><span>{error.message}</span></button>
              ))}
              {!validationIssues.length && <p>暂无校验问题</p>}
            </div>
          </aside>
          </fieldset>
        </div>

        <footer className="opc-profile-footer">
          <div>
            <span>{readOnly ? 'REFERENCE' : dirty ? 'UNSAVED' : revision ? `REV ${revision.slice(0, 10)}` : 'NEW FILE'}</span>
            <small>{fileName ? `${profile.name || fileName}` : '请选择配置文件'}</small>
          </div>
          {readOnly ? (
            <button className="opc-command" disabled={busy} onClick={onClose} type="button">关闭</button>
          ) : (
            <>
              <button disabled={busy} onClick={() => onSave('draft', false)} type="button">保存草稿</button>
              <button className="opc-command" disabled={busy || errors.length > 0} onClick={() => onSave('runnable', false)} type="button">校验并保存</button>
              <button className="opc-command" disabled={busy || errors.length > 0} onClick={() => onSave('runnable', true)} type="button">保存并下载</button>
              <button className="opc-reload" disabled={!revision || busy} onClick={onReload} type="button">Reload</button>
            </>
          )}
        </footer>
      </div>
    </div>
  );
}
