<template>
  <section class="page" data-module="metastore">
    <header class="page-head">
      <div>
        <h2>元数据存储管理</h2>
        <p class="page-desc">维护ETL涉及的元数据信息，包括表结构、分层归属和数据量级；字段一致性读取SQL模板侧的输出列校验快照。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="openCreate">登记元数据表</button>
        <button class="btn" type="button" @click="exportRows">导出元数据存储清单</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <form class="filter-bar" @submit.prevent="reload">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :placeholder="`按${field}检索`" />
      </label>
      <button class="btn" type="submit">查询</button>
      <button class="btn ghost" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>字段一致性</th>
          <th>引用模板</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)">
          <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
          <td :class="consistencyClass(row.字段一致性)">{{ row.字段一致性 ?? '未校验' }}</td>
          <td>{{ (row.引用模板 as string[] | undefined)?.join('、') || '—' }}</td>
          <td class="row-actions">
            <button
              v-for="action in actions"
              :key="action"
              class="link"
              type="button"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
            <button class="link" type="button" @click="showSnapshots(row)">校验快照</button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 3" class="empty-state">暂无元数据存储数据，可先登记元数据表</td>
        </tr>
      </tbody>
    </table>

    <section v-if="snapshotPanel" class="panel">
      <header class="panel-head">
        <h3>{{ snapshotPanel.表名 }} · 输出列校验数据快照</h3>
        <span :class="consistencyClass(snapshotPanel.字段一致性)">
          字段一致性：{{ snapshotPanel.字段一致性 }}
        </span>
      </header>
      <table class="data-table compact">
        <thead>
          <tr><th>模板编号</th><th>校验版本</th><th>校验状态</th><th>字段一致性</th><th>输出列顺序</th></tr>
        </thead>
        <tbody>
          <tr v-for="(item, index) in snapshotPanel.snapshots" :key="String(item.校验版本 ?? index)">
            <td>{{ item.模板编号 || '—' }}</td>
            <td>V{{ item.校验版本 }}</td>
            <td :class="item.passed ? '' : 'error-text'">{{ item.校验状态 }}</td>
            <td :class="consistencyClass(item.字段一致性)">{{ item.字段一致性 }}</td>
            <td>{{ (item.输出列 || []).join(' → ') || '—' }}</td>
          </tr>
          <tr v-if="!snapshotPanel.snapshots.length">
            <td colspan="5" class="empty-state">暂无SQL模板引用该表，未生成校验快照</td>
          </tr>
        </tbody>
      </table>
    </section>

    <footer class="page-foot">
      <span>共 {{ total }} 条元数据存储记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, unknown>

type SnapshotItem = {
  模板编号?: string
  校验版本?: number
  校验状态?: string
  字段一致性?: string
  passed?: boolean
  输出列?: string[]
}

type SnapshotPanel = {
  表名?: string
  字段一致性?: string
  snapshots: SnapshotItem[]
}

const ENDPOINT = '/api/metastore'
const columns = ["表编号", "表名", "所属数仓层", "字段数量", "分区字段", "主键字段", "数据量级", "最近更新时间"]
const actions = ["提交元数据评审", "确认元数据", "标记过期"]
const statuses = ["草稿", "元数据评审中", "已确认", "已过期"]
const stats = [{"label": "元数据表总数", "value": 0}, {"label": "已确认表", "value": 0}, {"label": "评审中表", "value": 0}]

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const snapshotPanel = ref<SnapshotPanel | null>(null)
const filters = ref<Record<string, string>>({})
const filterFields = columns.slice(0, 3)

function consistencyClass(value: unknown) {
  if (value === '一致') return 'ok-text'
  if (value === '不一致') return 'error-text'
  return 'hint-text'
}

async function showSnapshots(row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/output-column-snapshots`)
    if (!response.ok) {
      throw new Error('输出列校验快照读取失败')
    }
    snapshotPanel.value = await response.json()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '输出列校验快照读取失败'
  }
}

function resetFilters() {
  filters.value = {}
  void reload()
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

function openCreate() {
  errorMessage.value = '元数据表登记入口尚未接入审批流'
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ action }),
    })
    if (!response.ok) {
      throw new Error('元数据存储动作未生效，请稍后重试')
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '元数据存储操作失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const response = await request(`${ENDPOINT}?${query}`)
    if (!response.ok) {
      throw new Error('元数据表列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '元数据存储列表读取失败'
  }
}

onMounted(reload)
</script>
