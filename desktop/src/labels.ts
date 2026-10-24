export const STAGE_LABELS: Record<string, string> = {
  import: '导入报纸清单',
  issues: '枚举期次',
  boards: '抓取版面',
  articles: '抓取报道',
};

const OPERATION_LABELS: Record<string, string> = {
  ...STAGE_LABELS,
  login: '登录',
  'bark-test': '测试推送',
  test_bark: '测试推送',
  shutdown: '退出应用',
};

export function stageLabel(stage: string): string {
  return STAGE_LABELS[stage] ?? stage;
}

export function operationLabel(
  operation: string | null,
  stage?: string | null,
): string {
  if (!operation) {
    return '空闲';
  }
  if (operation === 'run') {
    return stage ? stageLabel(stage) : '爬取任务';
  }
  const base = operation.split(/[:\s]/)[0] ?? operation;
  return OPERATION_LABELS[base] ?? operation;
}
