import { relaunch } from '@tauri-apps/plugin-process';
import { check } from '@tauri-apps/plugin-updater';
import type { UpdateDownloadEvent, UpdateHandle } from './updater';

export async function checkForDesktopUpdate(): Promise<UpdateHandle | null> {
  const update = await check();
  if (!update) {
    return null;
  }
  return {
    version: update.version,
    body: update.body ?? null,
    date: update.date ?? null,
    async downloadAndInstall(onEvent) {
      await update.downloadAndInstall((event) => {
        onEvent(event as UpdateDownloadEvent);
      });
    },
  };
}

export async function relaunchDesktop(): Promise<void> {
  await relaunch();
}
