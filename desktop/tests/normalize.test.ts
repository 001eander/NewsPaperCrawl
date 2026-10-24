import { describe, expect, it } from 'vitest';
import {
  barkTestOutcome,
  normalizeConfig,
  normalizeFailed,
  normalizePaths,
  normalizeStatus,
} from '../src/normalize';

describe('normalizeStatus', () => {
  it('解析 Python sidecar 的 overview 与三元组 per_paper', () => {
    const raw = {
      overview: [
        {
          stage: 'issues',
          exists: true,
          total: 3,
          pending: 1,
          done: 1,
          failed: 1,
        },
        { stage: 'articles', exists: false },
      ],
      per_paper: [
        ['n.D1', '报纸一', ['1/2', '1/1', '--']],
        ['n.D2', '报纸二', ['0/1', '--', '--']],
      ],
    };

    const { overview, perPaper } = normalizeStatus(raw);

    expect(overview).toEqual([
      {
        stage: 'issues',
        exists: true,
        total: 3,
        done: 1,
        pending: 1,
        failed: 1,
      },
      {
        stage: 'articles',
        exists: false,
        total: 0,
        done: 0,
        pending: 0,
        failed: 0,
      },
    ]);
    expect(perPaper).toEqual([
      {
        paperid: 'n.D1',
        name: '报纸一',
        issues: '1/2',
        boards: '1/1',
        articles: '--',
      },
      {
        paperid: 'n.D2',
        name: '报纸二',
        issues: '0/1',
        boards: '--',
        articles: '--',
      },
    ]);
  });

  it('兼容 processing 计数与对象形式的按报纸进度', () => {
    const raw = {
      overview: {
        issues: { total: 4, done: 2, pending: 1, processing: 1 },
      },
      per_paper: [
        {
          paperid: 'n.D9',
          name: '测试报',
          stages: {
            issues: { done: 2, total: 4 },
            boards: { done: 1 },
          },
        },
      ],
    };

    const { overview, perPaper } = normalizeStatus(raw);

    expect(overview[0]).toMatchObject({
      stage: 'issues',
      total: 4,
      done: 2,
      pending: 2,
    });
    expect(perPaper[0]).toMatchObject({
      issues: '2/4',
      boards: '1',
      articles: '--',
    });
  });
});

describe('normalizeFailed', () => {
  it('将表名到元组数组的映射展开为失败行', () => {
    const rows = normalizeFailed({
      failed: {
        issues: [['n.D1', '2026-01-01', '第一行 第二行']],
        boards: [],
      },
    });

    expect(rows).toEqual([
      {
        table: 'issues',
        paperid: 'n.D1',
        date: '2026-01-01',
        error: '第一行 第二行',
      },
    ]);
  });

  it('兼容平铺对象列表', () => {
    const rows = normalizeFailed({
      failed: [
        {
          table: 'articles',
          paperid: 'n.D2',
          date: '2026-02-01',
          error: 'timeout',
        },
      ],
    });

    expect(rows[0]).toMatchObject({ table: 'articles', error: 'timeout' });
  });
});

describe('normalizeConfig', () => {
  it('读取 sidecar 的 credentials / bark 嵌套结构', () => {
    expect(
      normalizeConfig({
        credentials: { username: 'reader', password: 'secret' },
        bark: { device_key: 'key-1', bark_server: 'https://bark.example' },
      }),
    ).toEqual({
      username: 'reader',
      password: 'secret',
      deviceKey: 'key-1',
      barkServer: 'https://bark.example',
    });
  });

  it('过滤模板占位符并回退默认服务器', () => {
    const config = normalizeConfig({
      credentials: {
        username: '在此填入登录账号',
        password: '在此填入登录密码',
      },
      bark: { device_key: '', bark_server: '' },
    });

    expect(config).toEqual({
      username: '',
      password: '',
      deviceKey: '',
      barkServer: 'https://api.day.app',
    });
  });

  it('解析 Bark 测试结果', () => {
    expect(barkTestOutcome({ configured: false }).ok).toBe(false);
    expect(barkTestOutcome({ configured: true }).ok).toBe(true);
    expect(barkTestOutcome(null).ok).toBe(true);
  });
});

describe('normalizePaths', () => {
  it('忽略 sidecar 额外路径字段', () => {
    expect(
      normalizePaths({
        data_dir: '/tmp/data',
        log_dir: '/tmp/logs',
        secrets_dir: '/tmp/secrets',
        paperlist_csv: '/tmp/报纸清单.csv',
        browser_dir: '/tmp/browsers',
      }),
    ).toEqual({
      data_dir: '/tmp/data',
      log_dir: '/tmp/logs',
      secrets_dir: '/tmp/secrets',
    });
  });
});
