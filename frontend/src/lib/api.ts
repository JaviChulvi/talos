export class ApiError extends Error {
  constructor(message: string, readonly status: number) {
    super(message);
  }
}

export async function api<T>(path: string, options: RequestInit = {}): Promise<T> {
  const response = await fetch(`/api/v1${path}`, {
    cache: "no-store",
    ...options,
    headers: { "Content-Type": "application/json", ...options.headers },
  });
  const data: unknown = await response.json().catch(() => null);
  if (!response.ok) {
    const detail = data && typeof data === "object" && "detail" in data ? data.detail : null;
    const message = typeof detail === "string" ? detail : Array.isArray(detail)
      ? detail.map((entry: { msg?: string }) => entry.msg ?? "Invalid request").join("; ")
      : `The request failed (HTTP ${response.status}).`;
    throw new ApiError(message, response.status);
  }
  return data as T;
}

export function errorMessage(error: unknown) {
  return error instanceof Error ? error.message : "Unable to reach the local API.";
}
