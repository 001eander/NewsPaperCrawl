import type { AppContext } from '../context';
import { byId, errorMessage, setBusy } from '../dom';
import { barkTestOutcome, normalizeConfig, normalizePaths } from '../normalize';
import type { Paths } from '../types';

export interface SettingsViewApi {
  load(): Promise<void>;
}

export function createSettingsView(ctx: AppContext): SettingsViewApi {
  const credUsername = byId<HTMLInputElement>('cred-username');
  const credPassword = byId<HTMLInputElement>('cred-password');
  const credError = byId<HTMLParagraphElement>('cred-error');
  const saveCredentials = byId<HTMLButtonElement>('save-credentials');
  const togglePassword = byId<HTMLButtonElement>('toggle-password');

  const barkKey = byId<HTMLInputElement>('bark-key');
  const barkServer = byId<HTMLInputElement>('bark-server');
  const barkError = byId<HTMLParagraphElement>('bark-error');
  const saveBark = byId<HTMLButtonElement>('save-bark');
  const testBark = byId<HTMLButtonElement>('test-bark');

  const pathData = byId<HTMLElement>('path-data');
  const pathLogs = byId<HTMLElement>('path-logs');
  const pathSecrets = byId<HTMLElement>('path-secrets');
  const pathsError = byId<HTMLDivElement>('paths-error');

  const showError = (node: HTMLParagraphElement, message: string): void => {
    node.textContent = message;
    node.hidden = false;
  };

  const clearError = (node: HTMLParagraphElement): void => {
    node.textContent = '';
    node.hidden = true;
  };

  const renderPaths = (paths: Paths | null): void => {
    pathData.textContent = paths?.data_dir || '—';
    pathLogs.textContent = paths?.log_dir || '—';
    pathSecrets.textContent = paths?.secrets_dir || '—';
    if (paths) {
      pathsError.hidden = true;
      pathsError.textContent = '';
    }
  };

  const load = async (): Promise<void> => {
    try {
      const raw = await ctx.core.loadConfig();
      const config = normalizeConfig(raw);
      credUsername.value = config.username;
      credPassword.value = config.password;
      barkKey.value = config.deviceKey;
      barkServer.value = config.barkServer;
      clearError(credError);
      clearError(barkError);
    } catch (error) {
      const message = `配置加载失败：${errorMessage(error)}`;
      showError(credError, message);
      ctx.log(message);
    }
  };

  togglePassword.addEventListener('click', () => {
    const reveal = credPassword.type === 'password';
    credPassword.type = reveal ? 'text' : 'password';
    togglePassword.textContent = reveal ? '隐藏' : '显示';
  });

  saveCredentials.addEventListener('click', () => {
    void (async () => {
      const username = credUsername.value.trim();
      const password = credPassword.value.trim();
      if (!username || !password) {
        showError(credError, '账号和密码都不能为空');
        ctx.toast('账号和密码都不能为空', 'warning');
        (username ? credPassword : credUsername).focus();
        return;
      }

      setBusy(saveCredentials, true, '保存中…');
      try {
        await ctx.core.saveConfig('credentials', { username, password });
        clearError(credError);
        ctx.log('登录凭据已保存');
        ctx.toast('登录凭据已保存，下次登录时生效', 'success');
      } catch (error) {
        const message = `保存凭据失败：${errorMessage(error)}`;
        showError(credError, message);
        ctx.log(message);
        ctx.toast(message, 'error');
      } finally {
        setBusy(saveCredentials, false);
      }
    })();
  });

  const persistBark = async (): Promise<boolean> => {
    const device_key = barkKey.value.trim();
    const bark_server = barkServer.value.trim() || 'https://api.day.app';
    try {
      await ctx.core.saveConfig('bark', { device_key, bark_server });
      barkServer.value = bark_server;
      clearError(barkError);
      return true;
    } catch (error) {
      const message = `保存 Bark 配置失败：${errorMessage(error)}`;
      showError(barkError, message);
      ctx.log(message);
      ctx.toast(message, 'error');
      return false;
    }
  };

  saveBark.addEventListener('click', () => {
    void (async () => {
      setBusy(saveBark, true, '保存中…');
      try {
        if (await persistBark()) {
          ctx.log('Bark 配置已保存');
          ctx.toast('Bark 配置已保存，下次运行时生效', 'success');
        }
      } finally {
        setBusy(saveBark, false);
      }
    })();
  });

  testBark.addEventListener('click', () => {
    void (async () => {
      if (!barkKey.value.trim()) {
        showError(barkError, 'device key 为空，无法推送');
        ctx.toast('device key 为空，无法测试推送', 'warning');
        barkKey.focus();
        return;
      }

      setBusy(saveBark, true, '保存中…');
      const saved = await persistBark();
      if (!saved) {
        setBusy(saveBark, false);
        return;
      }
      setBusy(saveBark, false);
      setBusy(testBark, true, '发送中…');
      try {
        const raw = await ctx.core.testBark();
        const outcome = barkTestOutcome(raw);
        if (outcome.ok) {
          ctx.log('Bark 测试推送已发送');
          ctx.toast(
            outcome.message || '测试推送已发送，请查看手机',
            'success',
          );
        } else {
          const message = outcome.message || 'device key 无效，推送被跳过';
          ctx.log(message);
          ctx.toast(message, 'warning');
        }
      } catch (error) {
        const message = `测试推送失败：${errorMessage(error)}`;
        showError(barkError, message);
        ctx.log(message);
        ctx.toast(message, 'error');
      } finally {
        setBusy(testBark, false);
      }
    })();
  });

  renderPaths(ctx.store.get().paths);
  ctx.store.subscribe((snapshot) => {
    renderPaths(snapshot.paths);
  });

  return { load };
}

export function loadPaths(ctx: AppContext): Promise<void> {
  return ctx.core
    .getPaths()
    .then((raw) => {
      const paths = normalizePaths(raw);
      ctx.store.set({ paths });
      if (!paths) {
        throw new Error('后端未返回可用路径');
      }
    })
    .catch((error: unknown) => {
      const pathsError = byId<HTMLDivElement>('paths-error');
      pathsError.textContent = `读取本地目录失败：${errorMessage(error)}`;
      pathsError.hidden = false;
      ctx.log(`读取本地目录失败：${errorMessage(error)}`);
    });
}
