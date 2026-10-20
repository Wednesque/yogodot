// YogoDot Coding 伴侣 —— OpenCode 原生插件。
//
// 监听 OpenCode 的会话 / 权限 / 提问 / 工具事件，翻译成 YogoDot 标准事件，
// 异步 POST 到本机 http://127.0.0.1:8787/events。
//
// 安装：复制到 %USERPROFILE%\.config\opencode\plugins\（用户级）
//       或 <PROJECT>\.opencode\plugins\（项目级），重启 OpenCode。
//
// 隐私：只提取事件类型、session/turn/request 身份、工具名和状态；
// 不提取、不发送 prompt、消息正文、工具输入输出或模型回答。

import { createHash, randomUUID } from "node:crypto"
import { basename } from "node:path"

const SOURCE = "opencode"
const TIMEOUT_MS = 400
const MAX_SESSIONS = 64
const EVENT_URL = process.env.YOGO_COMPANION_URL ||
  `http://127.0.0.1:${process.env.YOGO_PORT || 8787}/events`

function text(value, maximum = 200) {
  if (typeof value === "string") return value.trim().slice(0, maximum)
  if (typeof value === "number" && Number.isFinite(value)) return String(value).slice(0, maximum)
  return ""
}

function first(...values) {
  for (const value of values) {
    const s = text(value, 4096)
    if (s) return s
  }
  return ""
}

function eventProperties(event) {
  return event?.properties || event?.data || {}
}

function nested(properties, ...names) {
  for (const name of names) {
    const value = properties?.[name]
    if (value && typeof value === "object" && !Array.isArray(value)) return value
  }
  return properties && typeof properties === "object" ? properties : {}
}

function rawSessionID(event) {
  const properties = eventProperties(event)
  const info = properties?.info || event?.info || {}
  const session = properties?.session || event?.session || {}
  const input = properties?.input || event?.input || {}
  const type = text(event?.type, 60).toLowerCase()
  const created = type.startsWith("session.")
    ? first(info.id, session.id, properties.id, event.id)
    : ""
  return first(
    event?.sessionID, event?.sessionId, event?.session_id,
    properties?.sessionID, properties?.sessionId, properties?.session_id,
    info?.sessionID, info?.sessionId, info?.session_id,
    input?.sessionID, input?.sessionId, input?.session_id,
    session?.id, session?.sessionID, session?.sessionId,
    properties?.permission?.sessionID, properties?.question?.sessionID,
    created,
  )
}

function sessionKey(raw) {
  const direct = `${SOURCE}:${raw}`
  if (direct.length <= 120) return direct
  return `${SOURCE}:${createHash("sha1").update(raw).digest("hex").slice(0, 20)}`
}

function turnOf(event) {
  const properties = eventProperties(event)
  const info = properties?.info || event?.info || {}
  const message = properties?.message || event?.message || info
  return text(
    event?.turnId || event?.turnID || event?.turn_id ||
    properties?.turnId || properties?.turnID || properties?.turn_id ||
    message?.turnId || message?.turnID || message?.turn_id ||
    (roleOf(event) === "user" ? (message?.id || info?.id) : ""),
    80,
  )
}

function roleOf(event) {
  const properties = eventProperties(event)
  const info = properties?.info || event?.info || {}
  const message = properties?.message || event?.message || info
  return text(message?.role || properties?.role || event?.role, 20).toLowerCase()
}

function requestOf(event) {
  const properties = eventProperties(event)
  const value = nested(properties, "permission", "approval", "question", "request", "info")
  const input = event?.input && typeof event.input === "object" ? event.input : {}
  return text(
    value?.requestId || value?.requestID || value?.request_id ||
    value?.permissionId || value?.permissionID || value?.permission_id ||
    value?.approvalId || value?.approvalID || value?.approval_id ||
    value?.questionId || value?.questionID || value?.question_id ||
    value?.toolUseId || value?.tool_use_id ||
    value?.callID || value?.callId || value?.call_id || value?.id ||
    input?.toolUseId || input?.tool_use_id ||
    input?.callID || input?.callId || input?.call_id,
    120,
  )
}

function toolOf(event) {
  const properties = eventProperties(event)
  const value = nested(properties, "permission", "approval", "tool", "input")
  const input = event?.input && typeof event.input === "object" ? event.input : {}
  return text(
    value?.operation || value?.toolName || value?.tool_name ||
    value?.name || value?.type ||
    input?.tool || input?.toolName || input?.tool_name,
    80,
  )
}

function statusOf(event) {
  const properties = eventProperties(event)
  const value = nested(properties, "permission", "approval")
  return text(
    value?.status || value?.response || value?.reply ||
    properties?.status || event?.status,
    30,
  ).toLowerCase()
}

function completionOf(type, event) {
  if (type === "session.idle") return "completed"
  if (type === "session.error") return "failed"
  if (type !== "session.status" && type !== "session.updated") return ""
  const properties = eventProperties(event)
  const status = properties?.status || event?.status || {}
  const value = text(
    typeof status === "object" ? (status.type || status.state) : status,
    30,
  ).toLowerCase()
  if (["idle", "complete", "completed", "done", "finished"].includes(value)) return "completed"
  if (["error", "failed", "failure"].includes(value)) return "failed"
  return ""
}

function allowedURL(value) {
  try {
    const url = new URL(value)
    return url.protocol === "http:" &&
      ["127.0.0.1", "localhost", "[::1]"].includes(url.hostname)
  } catch {
    return false
  }
}

export const YogoCompanionPlugin = async ({ project, directory, worktree } = {}) => {
  const sessions = new Map()
  let queue = Promise.resolve()

  function stateOf(sid) {
    let state = sessions.get(sid)
    if (!state) {
      state = {
        turnId: "",
        counter: 0,
        sequence: 0,
        pendingApproval: "",
        pendingSupplement: "",
        approvalPending: false,
        supplementPending: false,
      }
      sessions.set(sid, state)
      while (sessions.size > MAX_SESSIONS) {
        sessions.delete(sessions.keys().next().value)
      }
    } else {
      sessions.delete(sid)
      sessions.set(sid, state)
    }
    return state
  }

  function contextFor(event) {
    const properties = eventProperties(event)
    const info = properties?.info || event?.info || {}
    const cwd = text(info?.directory || properties?.directory || directory || worktree, 300)
    const name = text(project?.name || (cwd ? basename(cwd) : ""), 80)
    return { cwd, name }
  }

  function post(body) {
    if (!allowedURL(EVENT_URL)) return Promise.resolve()
    const controller = new AbortController()
    const timer = setTimeout(() => controller.abort(), TIMEOUT_MS)
    return fetch(EVENT_URL, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      signal: controller.signal,
    }).catch(() => {}).finally(() => clearTimeout(timer))
  }

  function enqueue(body) {
    queue = queue.then(() => post(body)).catch(() => {})
  }

  function send(name, sid, state, options = {}) {
    state.sequence += 1
    const context = options.context || { cwd: "", name: "" }
    const body = {
      schema: "yogo.companion.v1",
      event: name,
      sessionId: sid,
      turnId: options.turnId !== undefined ? options.turnId : state.turnId,
      eventId: randomUUID(),
      agent: SOURCE,
      occurredAt: Date.now(),
      sequence: state.sequence,
    }
    const data = {}
    if (options.message) data.message = text(options.message, 200)
    if (options.tool) data.tool = text(options.tool, 80)
    if (context.cwd) data.cwd = context.cwd
    if (context.name) {
      data.project = context.name
      data.name = context.name
    }
    if (Object.keys(data).length) body.data = data
    if (options.outcome) body.outcome = options.outcome
    if (options.interaction) body.interaction = options.interaction
    enqueue(body)
  }

  return {
    event: async ({ event } = {}) => {
      const type = text(event?.type, 80).toLowerCase()
      const raw = rawSessionID(event)
      if (!type || !raw) return
      const sid = sessionKey(raw)
      const state = stateOf(sid)
      const context = contextFor(event)

      if (type === "session.created") {
        state.turnId = ""
        state.counter = 0
        state.pendingApproval = ""
        state.pendingSupplement = ""
        state.approvalPending = false
        state.supplementPending = false
        send("session_started", sid, state, {
          context, turnId: "", message: "已连接",
        })
        return
      }
      if (type === "session.deleted") {
        send("session_ended", sid, state, { context, turnId: state.turnId })
        sessions.delete(sid)
        return
      }
      if (type === "message.updated" && roleOf(event) === "user") {
        const turn = turnOf(event)
        if (turn && turn === state.turnId) return
        if (turn) {
          state.turnId = turn
        } else {
          state.counter += 1
          state.turnId = `${sid.slice(0, 40)}:${state.counter}`.slice(0, 80)
        }
        state.pendingApproval = ""
        state.pendingSupplement = ""
        state.approvalPending = false
        state.supplementPending = false
        send("user_message_submitted", sid, state, { context, message: "开始处理" })
        return
      }

      if (["permission.asked", "permission.requested", "permission.updated",
           "approval.asked", "approval.requested"].includes(type)) {
        const requestId = requestOf(event)
        const tool = toolOf(event)
        state.pendingApproval = requestId
        state.approvalPending = true
        send("approval_requested", sid, state, {
          context, tool,
          message: tool ? `等待授权：${tool}` : "等待授权",
          interaction: { requestId, tool, kind: "authorization" },
        })
        return
      }
      if (["permission.replied", "permission.resolved",
           "approval.replied", "approval.updated"].includes(type)) {
        const requestId = requestOf(event) || state.pendingApproval
        const status = statusOf(event)
        state.pendingApproval = ""
        state.approvalPending = false
        send("processing_resumed", sid, state, {
          context, tool: toolOf(event),
          message: status === "denied" || status === "rejected"
            ? "授权被拒绝，继续处理" : "授权已处理，继续执行",
          interaction: requestId ? { requestId, kind: "authorization" } : undefined,
        })
        return
      }
      if (type === "question.asked") {
        const requestId = requestOf(event)
        state.pendingSupplement = requestId
        state.supplementPending = true
        send("supplement_requested", sid, state, {
          context, message: "等待补充",
          interaction: { requestId, kind: "supplement" },
        })
        return
      }
      if (type === "question.replied" || type === "question.rejected") {
        const requestId = requestOf(event) || state.pendingSupplement
        state.pendingSupplement = ""
        state.supplementPending = false
        send("processing_resumed", sid, state, {
          context, message: type === "question.rejected"
            ? "补充被拒绝，继续处理" : "补充已收到，继续执行",
          interaction: requestId ? { requestId, kind: "supplement" } : undefined,
        })
        return
      }

      const completion = completionOf(type, event)
      if (completion) {
        state.pendingApproval = ""
        state.pendingSupplement = ""
        state.approvalPending = false
        state.supplementPending = false
        send("turn_completed", sid, state, {
          context,
          message: completion === "failed" ? "失败" : "完成",
          outcome: completion === "failed" ? "failed" : "completed",
        })
      }
    },

    "tool.execute.before": async (input = {}, output = {}) => {
      const raw = text(input.sessionID || input.sessionId || input.session_id, 160)
      if (!raw) return
      const sid = sessionKey(raw)
      const state = stateOf(sid)
      if (!state.approvalPending) return
      const requestId = state.pendingApproval
      state.pendingApproval = ""
      state.approvalPending = false
      const tool = text(input.tool || input.toolName || output.tool, 80)
      send("processing_resumed", sid, state, {
        context: { cwd: "", name: "" },
        tool, message: tool ? `执行：${tool}` : "执行中",
        interaction: { requestId, kind: "authorization" },
      })
    },
  }
}

export default YogoCompanionPlugin
