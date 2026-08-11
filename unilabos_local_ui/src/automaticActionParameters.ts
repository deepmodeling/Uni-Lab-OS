const AUTOMATIC_S04_POSITION_METHODS = new Set([
  'submit_place_to_s04',
  'run_stirring',
  'submit_pick_from_s04',
]);

const AUTOMATIC_S04_POSITION_NODE_IDS = new Set([
  'w04_place_beaker_s04',
  'w04_run_stirring_s04',
  'w06_pick_beaker_s04',
]);

export function isAutomaticallyManagedActionParameter(
  method: string | undefined,
  parameter: string,
  nodeId = '',
) {
  const normalizedMethod = (method || '').split('.').pop() || '';
  return (
    parameter === 'position'
    && (
      AUTOMATIC_S04_POSITION_METHODS.has(normalizedMethod)
      || AUTOMATIC_S04_POSITION_NODE_IDS.has(nodeId)
      || /(?:^|_)s04(?:_|$)/i.test(nodeId)
    )
  ) || parameter === '瓶盖暂存位';
}

export function stripAutomaticallyManagedActionParameters(
  method: string | undefined,
  parameters: Record<string, unknown>,
  nodeId = '',
) {
  return Object.fromEntries(
    Object.entries(parameters).filter(
      ([parameter]) => !isAutomaticallyManagedActionParameter(method, parameter, nodeId),
    ),
  );
}

export function automaticActionParameterDescription(
  method: string | undefined,
  parameter: string,
  nodeId = '',
) {
  if (
    parameter === 'position'
    && (method === 'submit_place_to_s04' || nodeId === 'w04_place_beaker_s04')
  ) {
    return '磁搅位置 · 根据传感器自动选择首个空闲且就绪的 S04 工位';
  }
  if (parameter === 'position') {
    return '磁搅位置 · 自动沿用同一样品实际分配的 S04 工位';
  }
  if (parameter === '瓶盖暂存位') {
    return '瓶盖暂存位 · 开盖时自动选择空位，关盖时按样品 ID 找回对应瓶盖';
  }
  return '';
}
