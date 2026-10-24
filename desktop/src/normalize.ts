import type {
  FailedRow,
  LoadedConfig,
  OverviewRow,
  Paths,
  PerPaperRow,
} from './types';

const STAGES = ['issues', 'boards', 'articles'] as const;
const DEFAULT_BARK_SERVER = 'https://api.day.app';

type UnknownRecord = Record<string, unknown>;

export interface NormalizedStatus {
  overview: OverviewRow[];
  perPaper: PerPaperRow[];
}

export function isRecord(value: unknown): value is UnknownRecord {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function toRecord(value: unknown): UnknownRecord | null {
  if (isRecord(value)) {
    return value;
  }
  if (typeof value === 'string' && value.trim()) {
    try {
      const parsed: unknown = JSON.parse(value);
      return isRecord(parsed) ? parsed : null;
    } catch {
      return null;
    }
  }
  return null;
}

function stringValue(...values: unknown[]): string {
  for (const value of values) {
    if (typeof value === 'string' && value.trim()) {
      return value;
    }
    if (typeof value === 'number' && Number.isFinite(value)) {
      return String(value);
    }
  }
  return '';
}

function optionalNumber(...values: unknown[]): number | null {
  for (const value of values) {
    if (typeof value === 'number' && Number.isFinite(value)) {
      return value;
    }
    if (typeof value === 'string') {
      const trimmed = value.trim();
      if (trimmed && /^-?\d+(?:\.\d+)?$/.test(trimmed)) {
        const parsed = Number(trimmed);
        if (Number.isFinite(parsed)) {
          return parsed;
        }
      }
    }
  }
  return null;
}

function numberValue(...values: unknown[]): number {
  return optionalNumber(...values) ?? 0;
}

function firstDefined(...values: unknown[]): unknown {
  return values.find((value) => value !== undefined && value !== null);
}

function stageProgress(entry: UnknownRecord, stage: string): unknown {
  const done = optionalNumber(
    entry[`${stage}_done`],
    entry[`${stage}Done`],
    entry[`done_${stage}`],
  );
  const total = optionalNumber(
    entry[`${stage}_total`],
    entry[`${stage}Total`],
    entry[`total_${stage}`],
  );
  if (done === null && total === null) {
    return undefined;
  }
  return { done, total };
}

function cellDisplay(value: unknown): string {
  if (value === undefined || value === null) {
    return '--';
  }
  if (typeof value === 'string') {
    return value.trim() || '--';
  }
  if (typeof value === 'number') {
    return Number.isFinite(value) ? String(value) : '--';
  }
  if (typeof value === 'boolean') {
    return '--';
  }
  if (Array.isArray(value)) {
    if (value.length === 0) {
      return '--';
    }
    return value.map(cellDisplay).join('/');
  }
  if (isRecord(value)) {
    const display = stringValue(value.display, value.label, value.text);
    if (display) {
      return display;
    }

    const done = optionalNumber(value.done, value.completed, value.finished);
    let total = optionalNumber(value.total, value.count, value.all);
    if (done !== null || total !== null) {
      if (done === null) {
        return `?/${total ?? '?'}`;
      }
      if (total === null) {
        return String(done);
      }
      return `${done}/${total}`;
    }

    const statuses = isRecord(value.statuses) ? value.statuses : value;
    const sdone = optionalNumber(statuses.done, statuses.completed);
    if (sdone !== null) {
      const sp = numberValue(statuses.processing);
      const spPending = numberValue(statuses.pending);
      const sf = numberValue(statuses.failed);
      total = sp + spPending + sf;
      return total > 0 ? `${sdone}/${sdone + total}` : String(sdone);
    }

    if (value.cells !== undefined) {
      return cellDisplay(value.cells);
    }
    if (value.value !== undefined) {
      return cellDisplay(value.value);
    }
  }
  return '--';
}

function sortOverview(rows: OverviewRow[]): OverviewRow[] {
  const order = new Map<string, number>(STAGES.map((stage, index) => [stage, index]));
  return rows.sort(
    (left, right) => (order.get(left.stage) ?? 99) - (order.get(right.stage) ?? 99),
  );
}

function normalizeOverviewEntry(entry: unknown, index: number): OverviewRow {
  if (Array.isArray(entry)) {
    const stage = stringValue(entry[0]) || STAGES[index] || `stage_${index + 1}`;
    const total = numberValue(entry[1]);
    const done = numberValue(entry[2]);
    const pending = numberValue(entry[3]);
    const failed = numberValue(entry[4]);
    return {
      stage,
      exists: true,
      total: total || done + pending + failed,
      done,
      pending,
      failed,
    };
  }

  if (!isRecord(entry)) {
    return {
      stage: STAGES[index] ?? `stage_${index + 1}`,
      exists: false,
      total: 0,
      done: 0,
      pending: 0,
      failed: 0,
    };
  }

  const counts = isRecord(entry.counts) ? entry.counts : entry;
  const exists = entry.exists !== false;
  const done = numberValue(counts.done, entry.done, entry.completed);
  const pending =
    numberValue(counts.pending, entry.pending) +
    numberValue(counts.processing, entry.processing);
  const failed = numberValue(counts.failed, entry.failed);
  const explicitTotal = optionalNumber(entry.total, counts.total);
  const sum = done + pending + failed;
  const total = explicitTotal !== null && explicitTotal > 0 ? explicitTotal : sum;

  return {
    stage: stringValue(entry.stage, entry.table, entry.name) || STAGES[index] || `stage_${index + 1}`,
    exists,
    total,
    done,
    pending,
    failed,
  };
}

function normalizeOverview(source: unknown): OverviewRow[] {
  if (Array.isArray(source)) {
    return sortOverview(source.map((entry, index) => normalizeOverviewEntry(entry, index)));
  }
  if (!isRecord(source)) {
    return [];
  }
  if (Array.isArray(source.rows)) {
    return normalizeOverview(source.rows);
  }
  if (Array.isArray(source.overview)) {
    return normalizeOverview(source.overview);
  }

  const rows: OverviewRow[] = [];
  for (const [stage, value] of Object.entries(source)) {
    if (isRecord(value)) {
      rows.push(
        normalizeOverviewEntry(
          { ...value, stage: value.stage ?? stage },
          rows.length,
        ),
      );
    } else if (Array.isArray(value)) {
      rows.push(normalizeOverviewEntry([stage, ...value], rows.length));
    } else {
      rows.push(normalizeOverviewEntry({ stage, total: numberValue(value) }, rows.length));
    }
  }
  return sortOverview(rows);
}

function normalizePerPaperEntry(
  entry: unknown,
  fallbackPaperid = '',
): PerPaperRow | null {
  if (Array.isArray(entry)) {
    const paperid = stringValue(entry[0]) || fallbackPaperid;
    if (!paperid) {
      return null;
    }
    const nestedCells =
      entry.length >= 3 && Array.isArray(entry[2]) && entry[2].length >= 3
        ? entry[2]
        : null;
    return {
      paperid,
      name: stringValue(entry[1]),
      issues: cellDisplay(nestedCells ? nestedCells[0] : entry[2]),
      boards: cellDisplay(nestedCells ? nestedCells[1] : entry[3]),
      articles: cellDisplay(nestedCells ? nestedCells[2] : entry[4]),
    };
  }

  if (!isRecord(entry)) {
    return null;
  }

  const paperid = stringValue(
    entry.paperid,
    entry.paperId,
    entry.id,
    fallbackPaperid,
  );
  if (!paperid) {
    return null;
  }

  const stages = isRecord(entry.stages) ? entry.stages : null;
  let issues: unknown;
  let boards: unknown;
  let articles: unknown;

  if (Array.isArray(entry.cells)) {
    issues = entry.cells[0];
    boards = entry.cells[1];
    articles = entry.cells[2];
  } else {
    issues = firstDefined(
      entry.issues,
      stages?.issues,
      stageProgress(entry, 'issues'),
      entry.issue,
    );
    boards = firstDefined(
      entry.boards,
      stages?.boards,
      stageProgress(entry, 'boards'),
      entry.board,
    );
    articles = firstDefined(
      entry.articles,
      stages?.articles,
      stageProgress(entry, 'articles'),
      entry.article,
    );
  }

  return {
    paperid,
    name: stringValue(entry.name, entry.paper_name, entry.paperName, entry.title),
    issues: cellDisplay(issues),
    boards: cellDisplay(boards),
    articles: cellDisplay(articles),
  };
}

function normalizePerPaper(source: unknown): PerPaperRow[] {
  if (Array.isArray(source)) {
    return source
      .map((entry) => normalizePerPaperEntry(entry))
      .filter((row): row is PerPaperRow => row !== null);
  }
  if (!isRecord(source)) {
    return [];
  }

  const nested = firstDefined(
    source.rows,
    source.papers,
    source.per_paper,
    source.perPaper,
  );
  if (Array.isArray(nested)) {
    return normalizePerPaper(nested);
  }

  const rows: PerPaperRow[] = [];
  for (const [paperid, value] of Object.entries(source)) {
    if (Array.isArray(value)) {
      const row = normalizePerPaperEntry([paperid, ...value], paperid);
      if (row) {
        rows.push(row);
      }
      continue;
    }
    if (isRecord(value)) {
      const row = normalizePerPaperEntry(
        { ...value, paperid: firstDefined(value.paperid, value.id, paperid) },
        paperid,
      );
      if (row) {
        rows.push(row);
      }
    }
  }
  return rows;
}

export function normalizeStatus(raw: unknown): NormalizedStatus {
  const record = isRecord(raw) ? raw : {};
  return {
    overview: normalizeOverview(
      firstDefined(record.overview, record.overviewRows, record.overview_rows),
    ),
    perPaper: normalizePerPaper(
      firstDefined(
        record.per_paper,
        record.perPaper,
        record.per_papers,
        record.perPapers,
        record.papers,
      ),
    ),
  };
}

function errorText(value: unknown): string {
  if (typeof value === 'string') {
    return value;
  }
  if (typeof value === 'number' || typeof value === 'boolean') {
    return String(value);
  }
  if (isRecord(value)) {
    const message = stringValue(value.message, value.error, value.reason);
    if (message) {
      return message;
    }
    try {
      return JSON.stringify(value);
    } catch {
      return String(value);
    }
  }
  return '';
}

function normalizeFailedRow(item: unknown, fallbackTable = ''): FailedRow | null {
  if (Array.isArray(item)) {
    return {
      table: fallbackTable,
      paperid: stringValue(item[0]) || '—',
      date: stringValue(item[1]) || '—',
      error: errorText(item[2]).replace(/\s+/g, ' ').trim() || '—',
    };
  }
  if (!isRecord(item)) {
    return null;
  }
  return {
    table: stringValue(item.table, item.stage, fallbackTable),
    paperid: stringValue(item.paperid, item.paperId, item.id) || '—',
    date: stringValue(item.date, item.created_at, item.updated_at) || '—',
    error:
      errorText(firstDefined(item.error, item.message, item.last_error, item.reason))
        .replace(/\s+/g, ' ')
        .trim() || '—',
  };
}

function addFailedGroup(rows: FailedRow[], group: unknown, table: string): void {
  if (Array.isArray(group)) {
    for (const item of group) {
      const row = normalizeFailedRow(item, table);
      if (row) {
        rows.push(row);
      }
    }
    return;
  }
  if (isRecord(group) && Array.isArray(group.rows)) {
    for (const item of group.rows) {
      const row = normalizeFailedRow(item, table);
      if (row) {
        rows.push(row);
      }
    }
    return;
  }
  const row = normalizeFailedRow(group, table);
  if (row) {
    rows.push(row);
  }
}

export function normalizeFailed(raw: unknown): FailedRow[] {
  const record = isRecord(raw) ? raw : {};
  const source = record.failed ?? raw;
  const rows: FailedRow[] = [];

  if (Array.isArray(source)) {
    for (const item of source) {
      const row = normalizeFailedRow(item);
      if (row) {
        rows.push(row);
      }
    }
    return rows;
  }

  if (isRecord(source)) {
    let grouped = false;
    for (const table of STAGES) {
      const group = source[table];
      if (group !== undefined) {
        grouped = true;
        addFailedGroup(rows, group, table);
      }
    }
    if (!grouped) {
      for (const [table, group] of Object.entries(source)) {
        if (Array.isArray(group)) {
          grouped = true;
          addFailedGroup(rows, group, table);
        }
      }
    }
    if (!grouped) {
      const row = normalizeFailedRow(source);
      if (row) {
        rows.push(row);
      }
    }
  }

  return rows;
}

export function normalizePaths(raw: unknown): Paths | null {
  const record = toRecord(raw);
  if (!record) {
    return null;
  }
  const data_dir = stringValue(record.data_dir, record.dataDir);
  const log_dir = stringValue(record.log_dir, record.logDir);
  const secrets_dir = stringValue(record.secrets_dir, record.secretsDir);
  if (!data_dir && !log_dir && !secrets_dir) {
    return null;
  }
  return { data_dir, log_dir, secrets_dir };
}

function cleanSecret(value: string): string {
  const trimmed = value.trim();
  if (!trimmed || trimmed.includes('在此填入') || trimmed.includes('在此粘贴')) {
    return '';
  }
  return trimmed;
}

export function normalizeConfig(raw: unknown): LoadedConfig {
  const parsed = toRecord(raw) ?? {};
  const source = isRecord(parsed.config) ? parsed.config : parsed;

  const credentials = toRecord(source.credentials) ?? toRecord(source.credential) ?? source;
  const bark = toRecord(source.bark) ?? toRecord(source.bark_config) ?? source;
  const credentialValues = isRecord(credentials.values) ? credentials.values : credentials;
  const barkValues = isRecord(bark.values) ? bark.values : bark;

  return {
    username: cleanSecret(
      stringValue(
        credentialValues.username,
        credentialValues.account,
        credentialValues.user,
        source.username,
      ),
    ),
    password: cleanSecret(
      stringValue(
        credentialValues.password,
        credentialValues.pass,
        source.password,
      ),
    ),
    deviceKey: cleanSecret(
      stringValue(
        barkValues.device_key,
        barkValues.deviceKey,
        barkValues.key,
        source.device_key,
        source.deviceKey,
      ),
    ),
    barkServer:
      stringValue(
        barkValues.bark_server,
        barkValues.barkServer,
        barkValues.server,
        source.bark_server,
        source.barkServer,
      ).trim() || DEFAULT_BARK_SERVER,
  };
}

export interface BarkTestOutcome {
  ok: boolean;
  message: string;
}

export function barkTestOutcome(raw: unknown): BarkTestOutcome {
  if (raw === undefined || raw === null) {
    return { ok: true, message: '' };
  }
  if (typeof raw === 'boolean') {
    return { ok: raw, message: '' };
  }
  if (typeof raw === 'string') {
    return { ok: true, message: raw };
  }
  if (isRecord(raw)) {
    const configured = firstDefined(raw.sent, raw.ok, raw.success, raw.configured);
    return {
      ok: typeof configured === 'boolean' ? configured : true,
      message: stringValue(raw.message, raw.detail),
    };
  }
  return { ok: true, message: '' };
}
