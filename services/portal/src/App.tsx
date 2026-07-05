import { BrowserRouter, Navigate, Route, Routes } from 'react-router-dom'
import { getToken } from './api/client'
import Login from './pages/Login'
import ClaimsList from './pages/ClaimsList'
import ClaimDetail from './pages/ClaimDetail'

function RequireAuth({ children }: { children: React.ReactNode }) {
  return getToken() ? <>{children}</> : <Navigate to="/login" replace />
}

export default function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route path="/login" element={<Login />} />
        <Route
          path="/claims"
          element={<RequireAuth><ClaimsList /></RequireAuth>}
        />
        <Route
          path="/claims/:id"
          element={<RequireAuth><ClaimDetail /></RequireAuth>}
        />
        <Route path="*" element={<Navigate to="/claims" replace />} />
      </Routes>
    </BrowserRouter>
  )
}
