<template>
  <section class="page" data-module="sqltpl">
    <header class="page-head">
      <div>
        <h2>SQL模板管理</h2>
        <p class="page-desc">发布前把SQL输出列与目标表字段按数量、顺序、类型逐列比对，结论落入输出列校验数据快照。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="openCreate">登记模板定义</button>
        <button class="btn" type="button" @click="exportRows">导出SQL模板清单</button>
        <button class="btn" type="button" @click="toggleSnapshots">
          {{ showSnapshots ? '收起校验快照' : '查看校验快照' }}
        </button>
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
          <th>校验版本</th>
          <th>字段一致性</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)">
          <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
          <td>{{ row.校验版本 ?? '—' }}</td>
          <td :class="consistencyClass(row.字段一致性)">{{ row.字段一致性 ?? '未校验' }}</td>
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
            <button class="link" type="button" @click="validateRow(row)">校验输出列</button>
            <button class="link" type="button" @click="downloadRow(row)">下载</button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 3" class="empty-state">暂无SQL模板数据，可先登记模板定义</td>
        </tr>
      </tbody>
    </table>

    <section v-if="validation" class="panel">
      <header class="panel-head">
        <h3>输出列校验结果</h3>
        <span :class="validation.passed ? 'tag ok' : 'tag bad'">
          {{ validation.passed ? '通过' : '未通过' }}
        </span>
      </header>
      <p v-if="validation.问题?.length" class="error-text">问题：{{ validation.问题.join('、') }}</p>
      <p v-for="item in validation.附加问题" :key="item" class="hint-text">{{ item }}</p>
      <p v-for="item in validation.告警 || []" :key="item" class="hint-text">告警：{{ item }}</p>
      <p class="hint-text">
        字段数量 {{ validation.字段数量 }} / 目标 {{ validation.目标字段数量 }} ·
        目标表 {{ validation.目标表?.表名 || '—' }}
      </p>
      <table class="data-table compact">
        <thead>
          <tr><th>位置</th><th>输出列</th><th>输出类型</th><th>目标字段</th><th>目标类型</th></tr>
        </thead>
        <tbody>
          <tr v-for="(name, index) in validation.输出列" :key="`${name}-${index}`">
            <td>{{ index + 1 }}</td>
            <td>{{ name }}</td>
            <td>{{ validation.输出列类型?.[index] ?? '—' }}</td>
            <td>{{ validation.目标字段?.[index]?.name ?? '—' }}</td>
            <td>{{ validation.目标字段?.[index]?.data_type ?? '—' }}</td>
          </tr>
        </tbody>
      </table>
    </section>

    <section v-if="showSnapshots" class="panel">
      <header class="panel-head">
        <h3>输出列校验数据快照</h3>
        <span class="hint-text">同一模板文件重复载入只保留一个校验版本</span>
      </header>
      <table class="data-table compact">
        <thead>
          <tr><th>校验版本</th><th>模板编号</th><th>目标表</th><th>校验状态</th><th>字段一致性</th><th>输出列顺序</th></tr>
        </thead>
        <tbody>
          <tr v-for="(snapshot, index) in snapshots" :key="String(snapshot.校验版本 ?? index)">            <td>V{{ snapshot.校验版本 }}</td>
            <td>{{ snapshot.关联模板 || '—' }}</td>
            <td>{{ snapshot.目标表 || '—' }}</td>
            <td :class="snapshot.passed ? '' : 'error-text'">{{ snapshot.校验状态 }}</td>
            <td :class="consistencyClass(snapshot.字段一致性)">{{ snapshot.字段一致性 }}</td>
            <td>{{ (snapshot.输出列 || []).join(' → ') || '—' }}</td>
          </tr>
          <tr v-if="!snapshots.length">
            <td colspan="6" class="empty-state">暂无校验快照</td>
          </tr>
        </tbody>
      </table>
    </section>

    <footer class="page-foot">
      <span>共 {{ total }} 条SQL模板记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, string | number | boolean | null>

type ValidationView = {
  passed: boolean
  问题?: string[]
  附加问题?: string[]
  告警?: string[]
  字段数量?: number
  目标字段数量?: number
  目标表?: { 表名?: string }
  输出列?: string[]
  输出列类型?: string[]
  输出列顺序?: string[]
  目标字段?: { name: string; data_type: string }[]
}

type SnapshotView = {
  校验版本: number
  关联模板?: string
  目标表?: string
  校验状态?: string
  字段一致性?: string
  passed?: boolean
  输出列?: string[]
}

const ENDPOINT = '/api/sqltpl'
const columns = ["模板编号", "模板名称", "模板类型", "SQL片段", "参数占位符", "预期输出列", "参数默认值", "模板版本"]
const actions = ["提交模板评审", "发布模板", "废弃模板"]

const rows = ref<Row[]>([])
const snapshots = ref<SnapshotView[]>([])
const total = ref(0)
const errorMessage = ref('')
const validation = ref<ValidationView | null>(null)
const showSnapshots = ref(false)
const filters = ref<Record<string, string>>({})
const filterFields = columns.slice(0, 3)
const stats = ref([
  { label: '模板定义总数', value: 0 },
  { label: '已发布模板', value: 0 },
  { label: '评审中模板', value: 0 },
])

function consistencyClass(value: unknown) {
  if (value === '一致') return 'ok-text'
  if (value === '不一致') return 'error-text'
  return 'hint-text'
}

function resetFilters() {
  filters.value = {}
  void reload()
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

function openCreate() {
  errorMessage.value = '模板定义登记入口尚未接入审批流'
}

function downloadRow(row: Row) {
  window.open(`${ENDPOINT}/${row.id}/download`, '_blank')
}

async function validateRow(row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/validate`, { method: 'POST' })
    const payload = await response.json()
    validation.value = payload.validation ?? null
    if (!payload.ok) {
      errorMessage.value = payload.message ?? '输出列校验未通过'
    }
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '输出列校验失败'
  }
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ action }),
    })
    const payload = await response.json()
    if (!payload.ok) {
      // 发布校验未通过时后端会带回快照，直接展示逐列比对结果
      validation.value = payload.entry?.校验快照 ?? null
      throw new Error(payload.message ?? 'SQL模板动作未生效，请稍后重试')
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : 'SQL模板操作失败'
    await reload()
  }
}

function toggleSnapshots() {
  showSnapshots.value = !showSnapshots.value
  if (showSnapshots.value) {
    void reloadSnapshots()
  }
}

async function reloadSnapshots() {
  try {
    const response = await request(`${ENDPOINT}/snapshots?size=50`)
    const payload = await response.json()
    snapshots.value = payload.items ?? []
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '校验快照读取失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const response = await request(`${ENDPOINT}?${query}`)
    if (!response.ok) {
      throw new Error('模板定义列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
    stats.value = [
      { label: '模板定义总数', value: total.value },
      { label: '已发布模板', value: rows.value.filter((item) => item.status === '已发布').length },
      { label: '评审中模板', value: rows.value.filter((item) => item.status === '模板评审中').length },
    ]
    if (showSnapshots.value) {
      await reloadSnapshots()
    }
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : 'SQL模板列表读取失败'
  }
}

onMounted(reload)
</script>
