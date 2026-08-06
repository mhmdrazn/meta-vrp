// frontend/src/router.tsx
import { createBrowserRouter, RouterProvider } from 'react-router-dom'
import AppShell from './layouts/AppShell' // Layout utama Anda
import OptimizePage from './pages/OptimizePage'
import GroupsPage from './pages/GroupsPage'
import StatusPage from './pages/StatusPage'
import AssignPage from './pages/AssignPage'
import LogsPage from './pages/LogsPage'
import NodeEditorPage from './pages/NodeEditorPage'
import ResultsPage from './pages/ResultsPage'

const routes = [
  {
    path: '/',
    element: <AppShell />,
    children: [
      { path: '/', element: <OptimizePage /> },
      { path: '/results', element: <ResultsPage /> },
      { path: '/groups', element: <GroupsPage /> },
      { path: '/status', element: <StatusPage /> },
      { path: '/assign', element: <AssignPage /> },
      { path: '/logs', element: <LogsPage /> },
      { path: '/editor', element: <NodeEditorPage /> },
    ],
  },
]

// Buat instance router
const router = createBrowserRouter(routes)

// Buat komponen Router yang di-ekspor sebagai DEFAULT
export default function Router() {
  return <RouterProvider router={router} />
}
