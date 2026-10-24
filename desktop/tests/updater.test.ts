import { describe, expect, it, vi } from 'vitest';
import {
  UpdateController,
  type UpdateDownloadEvent,
  type UpdateHandle,
  type UpdaterDependencies,
} from '../src/updater';

function makeUpdate(
  overrides: Partial<UpdateHandle> = {},
): UpdateHandle {
  return {
    version: '0.3.0',
    body: '修复若干问题',
    date: '2026-10-24T00:00:00Z',
    downloadAndInstall: vi.fn(async () => {}),
    ...overrides,
  };
}

function makeDeps(
  overrides: Partial<UpdaterDependencies> = {},
): UpdaterDependencies {
  return {
    check: vi.fn(async () => null),
    prepareUpdate: vi.fn(async () => {}),
    relaunch: vi.fn(async () => {}),
    getOperation: vi.fn(async () => null),
    now: () => new Date('2026-10-24T12:00:00Z'),
    log: vi.fn(),
    ...overrides,
  };
}

describe('UpdateController', () => {
  it('静默检查无更新时进入 up-to-date，且不触碰 prepare_update', async () => {
    const deps = makeDeps();
    const controller = new UpdateController(deps);

    await controller.check({ silent: true });

    const state = controller.getState();
    expect(state.phase).toBe('up-to-date');
    expect(state.update).toBeNull();
    expect(state.error).toBeNull();
    expect(state.checkedAt).toBe('2026-10-24T12:00:00.000Z');
    expect(deps.prepareUpdate).not.toHaveBeenCalled();
  });

  it('发现更新时展示版本与说明，但不提前准备安装', async () => {
    const update = makeUpdate();
    const deps = makeDeps({ check: vi.fn(async () => update) });
    const controller = new UpdateController(deps);

    await controller.check({ silent: false });

    const state = controller.getState();
    expect(state.phase).toBe('available');
    expect(state.update).toEqual({
      version: '0.3.0',
      body: '修复若干问题',
      date: '2026-10-24T00:00:00Z',
    });
    expect(deps.prepareUpdate).not.toHaveBeenCalled();
    expect(update.downloadAndInstall).not.toHaveBeenCalled();
  });

  it('安装顺序为 prepare_update → downloadAndInstall → relaunch，并累计进度', async () => {
    const prepare = vi.fn(async () => {});
    const relaunch = vi.fn(async () => {});
    const download = vi.fn(async (onEvent: (event: UpdateDownloadEvent) => void) => {
      onEvent({ event: 'Started', data: { contentLength: 200 } });
      onEvent({ event: 'Progress', data: { chunkLength: 50 } });
      onEvent({ event: 'Progress', data: { chunkLength: 150 } });
      onEvent({ event: 'Finished' });
    });
    const update = makeUpdate({ downloadAndInstall: download });
    const deps = makeDeps({
      check: vi.fn(async () => update),
      prepareUpdate: prepare,
      relaunch,
    });
    const controller = new UpdateController(deps);

    await controller.check();
    expect(prepare).not.toHaveBeenCalled();

    const ok = await controller.install();

    expect(ok).toBe(true);
    expect(prepare).toHaveBeenCalledTimes(1);
    expect(download).toHaveBeenCalledTimes(1);
    expect(relaunch).toHaveBeenCalledTimes(1);
    expect(prepare.mock.invocationCallOrder[0]).toBeLessThan(
      download.mock.invocationCallOrder[0] ?? Number.POSITIVE_INFINITY,
    );
    expect(download.mock.invocationCallOrder[0]).toBeLessThan(
      relaunch.mock.invocationCallOrder[0] ?? Number.POSITIVE_INFINITY,
    );

    const state = controller.getState();
    expect(state.phase).toBe('restarting');
    expect(state.progress).toEqual({
      downloaded: 200,
      total: 200,
      percent: 100,
    });
  });

  it('手动检查失败进入 error，并保留错误信息', async () => {
    const deps = makeDeps({
      check: vi.fn(async () => {
        throw new Error('network down');
      }),
    });
    const controller = new UpdateController(deps);

    await controller.check({ silent: false });

    const state = controller.getState();
    expect(state.phase).toBe('error');
    expect(state.error).toContain('network down');
    expect(state.update).toBeNull();
  });

  it('静默检查失败只写日志，不向界面暴露错误', async () => {
    const log = vi.fn();
    const deps = makeDeps({
      check: vi.fn(async () => {
        throw new Error('offline');
      }),
      log,
    });
    const controller = new UpdateController(deps);

    await controller.check({ silent: true });

    const state = controller.getState();
    expect(state.phase).toBe('idle');
    expect(state.error).toBeNull();
    expect(log).toHaveBeenCalledWith(
      'warn',
      expect.stringContaining('offline'),
    );
  });

  it('活动任务存在时阻止安装，且不调用 prepare_update', async () => {
    const prepare = vi.fn(async () => {});
    const download = vi.fn(async () => {});
    const deps = makeDeps({
      check: vi.fn(async () => makeUpdate({ downloadAndInstall: download })),
      prepareUpdate: prepare,
      getOperation: vi.fn(async () => 'articles'),
    });
    const controller = new UpdateController(deps);

    await controller.check();
    const ok = await controller.install();

    expect(ok).toBe(false);
    const state = controller.getState();
    expect(state.phase).toBe('blocked');
    expect(state.error).toContain('articles');
    expect(state.error).toContain('任务');
    expect(prepare).not.toHaveBeenCalled();
    expect(download).not.toHaveBeenCalled();
  });

  it('prepare_update 失败时不开始下载', async () => {
    const download = vi.fn(async () => {});
    const deps = makeDeps({
      check: vi.fn(async () => makeUpdate({ downloadAndInstall: download })),
      prepareUpdate: vi.fn(async () => {
        throw new Error('sidecar busy');
      }),
    });
    const controller = new UpdateController(deps);

    await controller.check();
    const ok = await controller.install();

    expect(ok).toBe(false);
    expect(controller.getState().phase).toBe('error');
    expect(controller.getState().error).toContain('sidecar busy');
    expect(download).not.toHaveBeenCalled();
  });

  it('下载或安装失败时进入 error，不重启应用', async () => {
    const relaunch = vi.fn(async () => {});
    const deps = makeDeps({
      check: vi.fn(async () =>
        makeUpdate({
          downloadAndInstall: vi.fn(async () => {
            throw new Error('signature mismatch');
          }),
        }),
      ),
      relaunch,
    });
    const controller = new UpdateController(deps);

    await controller.check();
    const ok = await controller.install();

    expect(ok).toBe(false);
    expect(controller.getState().phase).toBe('error');
    expect(controller.getState().error).toContain('signature mismatch');
    expect(relaunch).not.toHaveBeenCalled();
  });

  it('没有更新时安装直接失败', async () => {
    const deps = makeDeps();
    const controller = new UpdateController(deps);

    const ok = await controller.install();

    expect(ok).toBe(false);
    expect(controller.getState().phase).toBe('error');
    expect(deps.prepareUpdate).not.toHaveBeenCalled();
  });
});
