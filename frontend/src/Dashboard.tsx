import { useState, useEffect, useReducer } from 'react'
import {
  Chart as ChartJS,
  CategoryScale,
  LinearScale,
  BarElement,
  LineElement,
  PointElement,
  Title,
  Tooltip,
  Legend,
} from 'chart.js'
import { Bar, Line } from 'react-chartjs-2'

ChartJS.register(
  CategoryScale,
  LinearScale,
  BarElement,
  LineElement,
  PointElement,
  Title,
  Tooltip,
  Legend,
)

const STORAGE_KEY = 'api_key'

// API Response types
interface ScoreBucket {
  bucket: string
  count: number
}

interface TaskStat {
  task: string
  avg_score: number
  attempts: number
}

interface TimelineEntry {
  date: string
  submissions: number
}

interface LabItem {
  lab: string
  task: string | null
  title: string
  type: string
}

// State types
type DashboardState =
  | { status: 'idle' }
  | { status: 'loading' }
  | { status: 'success'; data: DashboardData }
  | { status: 'error'; message: string }

interface DashboardData {
  scores: ScoreBucket[]
  timeline: TimelineEntry[]
  passRates: TaskStat[]
  labs: LabItem[]
}

type DashboardAction =
  | { type: 'fetch_start' }
  | { type: 'fetch_success'; data: DashboardData }
  | { type: 'fetch_error'; message: string }

function dashboardReducer(
  _state: DashboardState,
  action: DashboardAction,
): DashboardState {
  switch (action.type) {
    case 'fetch_start':
      return { status: 'loading' }
    case 'fetch_success':
      return { status: 'success', data: action.data }
    case 'fetch_error':
      return { status: 'error', message: action.message }
  }
}

// Lab options type
interface LabOption {
  value: string
  label: string
}

interface DashboardProps {
  onBack?: () => void
}

function Dashboard({ onBack }: DashboardProps) {
  const [token] = useState(() => localStorage.getItem(STORAGE_KEY) ?? '')
  const [selectedLab, setSelectedLab] = useState<string>('')
  const [state, dispatch] = useReducer(dashboardReducer, { status: 'idle' })
  const [labOptions, setLabOptions] = useState<LabOption[]>([])

  // Fetch labs list on mount
  useEffect(() => {
    if (!token) return

    fetch('/items/', {
      headers: { Authorization: `Bearer ${token}` },
    })
      .then((res) => {
        if (!res.ok) throw new Error(`HTTP ${res.status}`)
        return res.json()
      })
      .then((data: LabItem[]) => {
        // Extract unique labs from items
        const labs = data.filter((item) => item.type === 'lab')
        const options: LabOption[] = labs.map((lab) => ({
          value: lab.lab,
          label: lab.title,
        }))
        setLabOptions(options)
        if (options.length > 0 && !selectedLab) {
          setSelectedLab(options[0].value)
        }
      })
      .catch((err: Error) => {
        console.error('Failed to fetch labs:', err)
      })
  }, [token, selectedLab])

  // Fetch analytics data when lab changes
  useEffect(() => {
    if (!token || !selectedLab) return

    dispatch({ type: 'fetch_start' })

    const fetchAnalytics = async () => {
      try {
        const [scoresRes, timelineRes, passRatesRes] = await Promise.all([
          fetch(`/analytics/scores?lab=${selectedLab}`, {
            headers: { Authorization: `Bearer ${token}` },
          }),
          fetch(`/analytics/timeline?lab=${selectedLab}`, {
            headers: { Authorization: `Bearer ${token}` },
          }),
          fetch(`/analytics/pass-rates?lab=${selectedLab}`, {
            headers: { Authorization: `Bearer ${token}` },
          }),
        ])

        if (!scoresRes.ok) throw new Error(`HTTP ${scoresRes.status}`)
        if (!timelineRes.ok) throw new Error(`HTTP ${timelineRes.status}`)
        if (!passRatesRes.ok) throw new Error(`HTTP ${passRatesRes.status}`)

        const scores: ScoreBucket[] = await scoresRes.json()
        const timeline: TimelineEntry[] = await timelineRes.json()
        const passRates: TaskStat[] = await passRatesRes.json()

        dispatch({
          type: 'fetch_success',
          data: { scores, timeline, passRates, labs: [] },
        })
      } catch (err) {
        const message = err instanceof Error ? err.message : 'Unknown error'
        dispatch({ type: 'fetch_error', message })
      }
    }

    fetchAnalytics()
  }, [token, selectedLab])

  // Chart data preparation
  const scoreChartData = {
    labels: state.status === 'success' ? state.data.scores.map((s) => s.bucket) : [],
    datasets: [
      {
        label: 'Score Distribution',
        data: state.status === 'success' ? state.data.scores.map((s) => s.count) : [],
        backgroundColor: [
          'rgba(255, 99, 132, 0.6)',
          'rgba(255, 159, 64, 0.6)',
          'rgba(75, 192, 192, 0.6)',
          'rgba(54, 162, 235, 0.6)',
        ],
        borderColor: [
          'rgb(255, 99, 132)',
          'rgb(255, 159, 64)',
          'rgb(75, 192, 192)',
          'rgb(54, 162, 235)',
        ],
        borderWidth: 1,
      },
    ],
  }

  const timelineChartData = {
    labels: state.status === 'success' ? state.data.timeline.map((t) => t.date) : [],
    datasets: [
      {
        label: 'Submissions per Day',
        data: state.status === 'success' ? state.data.timeline.map((t) => t.submissions) : [],
        borderColor: 'rgb(54, 162, 235)',
        backgroundColor: 'rgba(54, 162, 235, 0.5)',
        tension: 0.1,
        fill: true,
      },
    ],
  }

  const chartOptions = {
    responsive: true,
    plugins: {
      legend: {
        position: 'top' as const,
      },
      title: {
        display: true,
        text: 'Analytics Overview',
      },
    },
  }

  if (!token) {
    return (
      <div className="dashboard">
        <p>Please connect with your API key to view the dashboard.</p>
      </div>
    )
  }

  return (
    <div className="dashboard">
      <header className="dashboard-header">
        <div style={{ display: 'flex', alignItems: 'center', gap: '1rem' }}>
          <h1>Dashboard</h1>
          {onBack && (
            <button onClick={onBack} className="btn-back">
              Back
            </button>
          )}
        </div>
        <div className="lab-selector">
          <label htmlFor="lab-select">Select Lab: </label>
          <select
            id="lab-select"
            value={selectedLab}
            onChange={(e) => setSelectedLab(e.target.value)}
          >
            {labOptions.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </select>
        </div>
      </header>

      {state.status === 'loading' && <p>Loading analytics...</p>}

      {state.status === 'error' && (
        <p className="error">Error: {state.message}</p>
      )}

      {state.status === 'success' && (
        <div className="dashboard-content">
          <section className="chart-section">
            <h2>Score Distribution</h2>
            <Bar data={scoreChartData} options={chartOptions} />
          </section>

          <section className="chart-section">
            <h2>Submissions Timeline</h2>
            <Line data={timelineChartData} options={chartOptions} />
          </section>

          <section className="table-section">
            <h2>Pass Rates by Task</h2>
            <table>
              <thead>
                <tr>
                  <th>Task</th>
                  <th>Average Score</th>
                  <th>Attempts</th>
                </tr>
              </thead>
              <tbody>
                {state.data.passRates.map((stat, index) => (
                  <tr key={index}>
                    <td>{stat.task}</td>
                    <td>{stat.avg_score.toFixed(1)}%</td>
                    <td>{stat.attempts}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </section>
        </div>
      )}
    </div>
  )
}

export default Dashboard
