import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { api, setToken } from '../api/client'

export default function Login() {
  const navigate = useNavigate()
  const [email, setEmail]       = useState('')
  const [password, setPassword] = useState('')
  const [error, setError]       = useState('')
  const [loading, setLoading]   = useState(false)
  const [focus, setFocus]       = useState<string | null>(null)

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault()
    setError('')
    setLoading(true)
    try {
      const resp = await api.login(email, password)
      setToken(resp.access_token)
      navigate('/claims', { replace: true })
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Sign in failed. Please check your credentials.')
    } finally {
      setLoading(false)
    }
  }

  const inputStyle = (field: string): React.CSSProperties => ({
    width: '100%',
    padding: '9px 13px',
    fontSize: 14,
    border: `1.5px solid ${focus === field ? '#2563eb' : '#e2e8f0'}`,
    borderRadius: 8,
    outline: 'none',
    color: '#0f172a',
    background: '#fff',
    boxShadow: focus === field ? '0 0 0 3px rgba(37,99,235,0.08)' : 'none',
    transition: 'border-color 0.15s, box-shadow 0.15s',
  })

  return (
    <div style={{
      minHeight: '100vh',
      display: 'flex', alignItems: 'center', justifyContent: 'center',
      background: 'linear-gradient(160deg, #0f172a 0%, #1e3a5f 55%, #0f172a 100%)',
    }}>
      <div style={{
        background: '#fff',
        borderRadius: 16,
        padding: '48px 44px',
        width: '100%',
        maxWidth: 420,
        boxShadow: '0 25px 60px rgba(0,0,0,0.4), 0 0 0 1px rgba(255,255,255,0.05)',
      }}>

        {/* Brand */}
        <div style={{ display: 'flex', alignItems: 'center', gap: 12, marginBottom: 28 }}>
          <div style={{
            width: 40, height: 40,
            background: 'linear-gradient(135deg, #2563eb, #4f46e5)',
            borderRadius: 9,
            display: 'flex', alignItems: 'center', justifyContent: 'center',
            color: '#fff', fontWeight: 800, fontSize: 14, letterSpacing: 0.5,
            flexShrink: 0,
          }}>IC</div>
          <div>
            <div style={{ fontWeight: 700, fontSize: 17, color: '#0f172a', lineHeight: 1.2 }}>
              Claims Portal
            </div>
            <div style={{ fontSize: 11.5, color: '#94a3b8', marginTop: 2 }}>
              Insurance Claims Processing System
            </div>
          </div>
        </div>

        <div style={{ height: 1, background: '#f1f5f9', marginBottom: 28 }} />

        <div style={{ fontSize: 15, fontWeight: 600, color: '#0f172a', marginBottom: 22 }}>
          Sign in to your account
        </div>

        {error && (
          <div style={{
            display: 'flex', alignItems: 'center', gap: 8,
            background: '#fef2f2', border: '1px solid #fecaca',
            borderRadius: 8, padding: '10px 14px',
            color: '#dc2626', fontSize: 13, marginBottom: 20,
          }}>
            <span style={{ fontSize: 16 }}>⚠</span>
            {error}
          </div>
        )}

        <form onSubmit={handleSubmit}>
          <div style={{ marginBottom: 16 }}>
            <label style={{ display: 'block', fontSize: 12, fontWeight: 600, color: '#374151', marginBottom: 6 }}>
              Email address
            </label>
            <input
              type="email"
              value={email}
              onChange={e => setEmail(e.target.value)}
              placeholder="you@example.com"
              required
              autoFocus
              onFocus={() => setFocus('email')}
              onBlur={() => setFocus(null)}
              style={inputStyle('email')}
            />
          </div>

          <div style={{ marginBottom: 28 }}>
            <label style={{ display: 'block', fontSize: 12, fontWeight: 600, color: '#374151', marginBottom: 6 }}>
              Password
            </label>
            <input
              type="password"
              value={password}
              onChange={e => setPassword(e.target.value)}
              placeholder="••••••••"
              required
              onFocus={() => setFocus('password')}
              onBlur={() => setFocus(null)}
              style={inputStyle('password')}
            />
          </div>

          <button
            type="submit"
            disabled={loading}
            style={{
              width: '100%',
              padding: '11px',
              background: loading ? '#93c5fd' : '#2563eb',
              color: '#fff',
              border: 'none',
              borderRadius: 8,
              fontSize: 14,
              fontWeight: 600,
              cursor: loading ? 'not-allowed' : 'pointer',
              transition: 'background 0.15s',
              letterSpacing: 0.1,
            }}
          >
            {loading ? 'Signing in…' : 'Sign In →'}
          </button>
        </form>

        <div style={{
          marginTop: 36,
          paddingTop: 20,
          borderTop: '1px solid #f1f5f9',
          textAlign: 'center',
          fontSize: 11,
          color: '#cbd5e1',
        }}>
          Unison Insurance · ICPS v1.0
        </div>
      </div>
    </div>
  )
}
