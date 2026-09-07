export type ApiResponse<T> = {
  ok: boolean;
  data: T | null;
  error: string | null;
};

export class ApiError extends Error {
  readonly status: number;
  readonly payload: unknown;

  constructor(message: string, status: number, payload: unknown = null) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.payload = payload;
  }
}

async function readPayload(response: Response): Promise<unknown> {
  try {
    return await response.json();
  } catch {
    return null;
  }
}

/** Normalize caller paths to the single public API prefix. */
function normalizeApiPath(path: string): string {
  const raw = path.trim().replace(/^\/+/, '');
  const resource = raw.startsWith('api/v1/') ? raw.slice('api/v1/'.length) : raw;
  return `/api/v1/${resource}`;
}

function assertApi<T>(response: Response, payload: unknown): ApiResponse<T> {
  const envelope = payload as Partial<ApiResponse<T>> | null;
  if (!response.ok || envelope?.ok !== true) {
    const message = typeof envelope?.error === 'string'
      ? envelope.error
      : `请求失败（${response.status}）`;
    throw new ApiError(message, response.status, payload);
  }
  return envelope as ApiResponse<T>;
}

export async function getApi<T>(path: string): Promise<ApiResponse<T>> {
  const response = await fetch(normalizeApiPath(path), {
    headers: { Accept: 'application/json' },
    credentials: 'same-origin',
  });
  return assertApi<T>(response, await readPayload(response));
}

export async function postApi<T>(
  path: string,
  payload: Record<string, unknown>,
  csrfToken: string,
): Promise<ApiResponse<T>> {
  const response = await fetch(normalizeApiPath(path), {
    method: 'POST',
    headers: {
      Accept: 'application/json',
      'Content-Type': 'application/json',
      'X-CSRF-Token': csrfToken,
    },
    credentials: 'same-origin',
    body: JSON.stringify({ ...payload, csrf_token: csrfToken }),
  });
  return assertApi<T>(response, await readPayload(response));
}

export async function patchApi<T>(
  path: string,
  payload: Record<string, unknown>,
  csrfToken: string,
): Promise<ApiResponse<T>> {
  const response = await fetch(normalizeApiPath(path), {
    method: 'PATCH',
    headers: {
      Accept: 'application/json',
      'Content-Type': 'application/json',
      'X-CSRF-Token': csrfToken,
    },
    credentials: 'same-origin',
    body: JSON.stringify({ ...payload, csrf_token: csrfToken }),
  });
  return assertApi<T>(response, await readPayload(response));
}

export async function deleteApi<T>(
  path: string,
  csrfToken: string,
): Promise<ApiResponse<T>> {
  const response = await fetch(normalizeApiPath(path), {
    method: 'DELETE',
    headers: {
      Accept: 'application/json',
      'X-CSRF-Token': csrfToken,
      'Content-Type': 'application/json',
    },
    credentials: 'same-origin',
    body: JSON.stringify({ csrf_token: csrfToken }),
  });
  return assertApi<T>(response, await readPayload(response));
}
