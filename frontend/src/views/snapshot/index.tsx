import { useCallback, useEffect, useMemo, useState } from 'react'

import { request } from '@/api/client'

const ENDPOINT = '/api/snapshot'
const columns = [
  "清单编号",
  "配置编号",
  "存储路径",
  "快照频率",
  "状态大小",
  "受保护文件",
  "可清理文件",
  "引用来源",
  "清单状态"
]
const actions = [
  "确认清单",
  "清理校验"
]
const statuses = [
  "待确认",
  "已确认",
  "已校验"
]

type Todo = { 文件: string; 判定: string; 状态: string; 结论: string }
type CellValue = string | number | string[] | null
type Row = Record<string, CellValue> & {
  id: number
  清理待办?: Todo[]
  处理结论?: string[]
}

function renderCell(value: CellValue | undefined): string {
  if (Array.isArray(value)) {
    return value.length > 0 ? value.join('、') : '—'
  }
  if (value === null || value === undefined || value === '') {
    return '—'
  }
  return String(value)
}

function countFiles(value: CellValue | undefined): number {
  return Array.isArray(value) ? value.length : 0
}

export default function SnapshotView() {
  const [rows, setRows] = useState<Row[]>([])
  const [total, setTotal] = useState(0)
  const [errorMessage, setErrorMessage] = useState('')
  const [infoMessage, setInfoMessage] = useState('')
  const [filters, setFilters] = useState<Record<string, string>>({})
  const [detail, setDetail] = useState<Row | null>(null)
  const filterFields = useMemo(() => columns.slice(0, 3), [])

  const stats = useMemo(() => ([
    { label: '快照清单总数', value: total },
    { label: '受保护文件', value: rows.reduce((sum, row) => sum + countFiles(row['受保护文件']), 0) },
    { label: '可清理文件', value: rows.reduce((sum, row) => sum + countFiles(row['可清理文件']), 0) },
  ]), [rows, total])

  const reload = useCallback(async () => {
    setErrorMessage('')
    const query = new URLSearchParams(filters).toString()
    try {
      const response = await request(`${ENDPOINT}?${query}`)
      if (!response.ok) {
        throw new Error('状态快照清单列表读取失败')
      }
      const payload = await response.json()
      setRows(payload.items ?? [])
      setTotal(payload.total ?? 0)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '状态快照清单列表读取失败')
    }
  }, [filters])

  useEffect(() => {
    void reload()
  }, [reload])

  async function runAction(action: string, row: Row) {
    setErrorMessage('')
    setInfoMessage('')
    try {
      const response = await request(`${ENDPOINT}/${row.id}/actions`, {
        method: 'POST',
        body: JSON.stringify({ values: { action } }),
      })
      const payload = await response.json()
      if (!response.ok || !payload.ok) {
        throw new Error(payload.message || '状态快照清单动作未生效，请稍后重试')
      }
      setInfoMessage(payload.message || '动作已生效')
      await reload()
      if (detail && detail.id === row.id) {
        await openDetail(row)
      }
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '状态快照清单操作失败')
    }
  }

  async function openDetail(row: Row) {
    setErrorMessage('')
    try {
      const response = await request(`${ENDPOINT}/${row.id}`)
      if (!response.ok) {
        throw new Error('清单明细读取失败')
      }
      setDetail(await response.json())
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '清单明细读取失败')
    }
  }

  return (
    <section className="page" data-module="snapshot">
      <header className="page-head">
        <div>
          <h2>状态快照清单</h2>
          <p className="page-desc">盘点状态后端存储路径下的快照文件，标记受保护文件、可清理文件与引用来源，清理校验只遵循最终确认的一份清单。</p>
        </div>
        <div className="page-actions">
          <button className="btn" type="button" onClick={() => window.open(`${ENDPOINT}/export`, '_blank')}>
            导出状态快照清单
          </button>
        </div>
      </header>

      <div className="stat-row">
        {stats.map((item) => (
          <article key={item.label} className="stat-card">
            <span className="stat-label">{item.label}</span>
            <strong className="stat-value">{item.value}</strong>
          </article>
        ))}
      </div>

      <form className="filter-bar" onSubmit={(event) => { event.preventDefault(); void reload() }}>
        {filterFields.map((field) => (
          <label key={field} className="filter-item">
            <span>{field}</span>
            <input
              value={filters[field] ?? ''}
              placeholder={`按${field}检索`}
              onChange={(event) => setFilters((prev) => ({ ...prev, [field]: event.target.value }))}
            />
          </label>
        ))}
        <button className="btn" type="submit">查询</button>
        <button className="btn ghost" type="button" onClick={() => setFilters({})}>重置条件</button>
      </form>

      <table className="data-table">
        <thead>
          <tr>
            {columns.map((column) => <th key={column}>{column}</th>)}
            <th>可执行动作</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={String(row.id)}>
              {columns.map((column) => <td key={column}>{renderCell(row[column])}</td>)}
              <td className="row-actions">
                {actions.map((action) => (
                  <button key={action} className="link" type="button" onClick={() => void runAction(action, row)}>
                    {action}
                  </button>
                ))}
                <button className="link" type="button" onClick={() => void openDetail(row)}>
                  待办详情
                </button>
              </td>
            </tr>
          ))}
          {rows.length === 0 && (
            <tr>
              <td className="empty-state" colSpan={columns.length + 1}>暂无状态快照清单，可先在状态后端管理页执行快照盘点</td>
            </tr>
          )}
        </tbody>
      </table>

      {detail && (
        <div className="detail-panel">
          <h3>清单 {detail['清单编号']} 的清理待办（与清单一同落库）</h3>
          <table className="data-table">
            <thead>
              <tr><th>文件</th><th>判定</th><th>状态</th><th>结论</th></tr>
            </thead>
            <tbody>
              {(detail.清理待办 ?? []).map((todo) => (
                <tr key={todo.文件}>
                  <td>{todo.文件}</td>
                  <td>{todo.判定}</td>
                  <td>{todo.状态}</td>
                  <td>{todo.结论 || '—'}</td>
                </tr>
              ))}
              {(detail.清理待办 ?? []).length === 0 && (
                <tr><td className="empty-state" colSpan={4}>该清单没有清理待办</td></tr>
              )}
            </tbody>
          </table>
          <h3>处理结论（跨流程回写保留）</h3>
          <ul className="conclusion-list">
            {(detail.处理结论 ?? []).map((item, index) => <li key={index}>{item}</li>)}
          </ul>
        </div>
      )}

      <footer className="page-foot">
        <span>共 {total} 条状态快照清单记录</span>
        {infoMessage && <span className="ok-text">{infoMessage}</span>}
        {errorMessage && <span className="error-text">{errorMessage}</span>}
      </footer>
    </section>
  )
}
