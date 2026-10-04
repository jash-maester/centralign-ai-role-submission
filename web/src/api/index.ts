import type { ApiClient } from './client';
import { HttpClient } from './http';

/**
 * The single API client. The mock adapter is bundled only when the build sets
 * VITE_MOCK=1 (`vite build --mode mock`); otherwise this module never imports
 * it, so the default production build talks to the real API under /api.
 */
export const IS_MOCK = import.meta.env.VITE_MOCK === '1';

let client: ApiClient = new HttpClient('/api');

export function api(): ApiClient {
  return client;
}

export function setApiClient(c: ApiClient): void {
  client = c;
}

export async function initApi(): Promise<ApiClient> {
  if (IS_MOCK) {
    const { MockClient } = await import('./mock/client');
    client = new MockClient();
  }
  return client;
}
