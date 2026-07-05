const API_URL = ''

const TOKEN_KEY = 'portal_token'

export function getToken(): string | null {
  return localStorage.getItem(TOKEN_KEY)
}

export function setToken(token: string): void {
  localStorage.setItem(TOKEN_KEY, token)
}

export function clearToken(): void {
  localStorage.removeItem(TOKEN_KEY)
}

async function request<T>(
  path: string,
  options: RequestInit = {},
): Promise<T> {
  const token = getToken()
  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
    ...(options.headers as Record<string, string> || {}),
  }
  if (token) {
    headers['Authorization'] = `Bearer ${token}`
  }

  const resp = await fetch(`${API_URL}${path}`, {
    ...options,
    headers,
  })

  if (resp.status === 401) {
    clearToken()
    window.location.href = '/login'
    throw new Error('Unauthorized')
  }

  if (!resp.ok) {
    const body = await resp.json().catch(() => ({ detail: resp.statusText }))
    throw new Error(body.detail || `HTTP ${resp.status}`)
  }

  return resp.json() as Promise<T>
}

export const api = {
  login: (email: string, password: string) =>
    request<{ access_token: string; token_type: string; user: object }>('/auth/login', {
      method: 'POST',
      body: JSON.stringify({ email, password }),
    }),

  me: () => request<object>('/auth/me'),

  claims: (page = 1, perPage = 20, status?: string) => {
    const params = new URLSearchParams({
      page: String(page),
      per_page: String(perPage),
    })
    if (status) params.set('status', status)
    return request<import('../types').ClaimsListResponse>(`/v1/dashboard/claims?${params}`)
  },

  claimCost: (id: string) =>
    request<import('../types').ClaimCostDetail>(`/v1/dashboard/claims/${id}/cost`),

  stats: () =>
    request<import('../types').DashboardStats>('/v1/dashboard/stats'),
}
