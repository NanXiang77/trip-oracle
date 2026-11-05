/**
 * trip-oracle 唤醒代理（Cloudflare Worker）
 *
 * 作用：华为云 AgentArts 把 MCP 地址指向本 Worker，而不是直接指向容器。
 *       容器停了（Stopped）时，Worker 会调 SnapDeploy 的 start 接口把它拉起来，
 *       再转发原来的 MCP 请求。
 *
 * 部署前提：
 *   1. Cloudflare 账号（免费，不用卡）
 *   2. 在 Worker 的 Settings → Variables 里配两个密钥：
 *        SNAPDEPLOY_API_KEY       sd_pat_...（scope: deploy 或 manage）
 *        SNAPDEPLOY_CONTAINER_ID  例如 2f65c79-9fca-4eed-9881-0c47683485d5
 *   3. 可选变量 BACKEND（默认指向 trip-oracle 容器）
 *
 * ⚠️ 已知限制（务必知道）：
 *   Cloudflare 免费版的单次请求有约 30 秒的墙钟上限，而容器冷启动要 30~60 秒。
 *   所以本 Worker 最多等 25 秒——
 *     · 容器是「自动休眠」时：通常 10~20 秒能醒，体验良好
 *     · 容器是「深度停止」时：可能等不到，会返回 503 提示"正在启动，请稍后重试"；
 *       但这次调用已经把容器拉起来了，用户/智能体重试一次就通
 *   真正稳妥的做法仍是：答辩前手动 Start 一次，Worker 只作为兜底。
 */

const DEFAULT_BACKEND = 'https://trip-oracle-dcf89.containers.snapdeploy.app';
const WAIT_SECONDS = 25;      // 免费版安全上限
const POLL_INTERVAL_MS = 4000;

const corsHeaders = {
  'Access-Control-Allow-Origin': '*',
  'Access-Control-Allow-Methods': 'GET, POST, OPTIONS',
  'Access-Control-Allow-Headers': '*',
};

function jsonError(message, status = 503) {
  return new Response(JSON.stringify({ error: message }), {
    status,
    headers: { 'Content-Type': 'application/json', ...corsHeaders },
  });
}

/** 探测后端是否真的就绪（返回 JSON 才算，避免把"Waking up"页面当成成功） */
async function probe(backend) {
  try {
    const r = await fetch(backend + '/health', {
      method: 'GET',
      headers: { 'User-Agent': 'trip-oracle-worker/1.0' },
    });
    if (!r.ok) return false;
    const text = (await r.text()).trimStart();
    return text.startsWith('{');
  } catch {
    return false;
  }
}

async function startContainer(env) {
  const key = env.SNAPDEPLOY_API_KEY;
  const cid = env.SNAPDEPLOY_CONTAINER_ID;
  if (!key || !cid) return { skipped: true };
  try {
    const r = await fetch(`https://snapdeploy.dev/api/mobile/containers/${cid}/start`, {
      method: 'POST',
      headers: {
        Authorization: `Bearer ${key}`,
        'Content-Type': 'application/json',
      },
    });
    const body = await r.text();
    return { status: r.status, body: body.slice(0, 300) };
  } catch (e) {
    return { error: String(e) };
  }
}

export default {
  async fetch(request, env) {
    if (request.method === 'OPTIONS') {
      return new Response(null, { status: 204, headers: corsHeaders });
    }

    const backend = (env.BACKEND || DEFAULT_BACKEND).replace(/\/$/, '');
    const url = new URL(request.url);

    // 前端预热用：浏览器打开这个地址就能把容器叫醒
    if (url.pathname === '/warmup') {
      let alive = await probe(backend);
      if (!alive) {
        await startContainer(env);
        const deadline = Date.now() + WAIT_SECONDS * 1000;
        while (Date.now() < deadline) {
          await new Promise((r) => setTimeout(r, POLL_INTERVAL_MS));
          if (await probe(backend)) { alive = true; break; }
        }
      }
      return new Response(JSON.stringify({ ready: alive, backend }), {
        status: alive ? 200 : 503,
        headers: { 'Content-Type': 'application/json', ...corsHeaders },
      });
    }

    // 正常请求：先确保后端活着
    let alive = await probe(backend);
    if (!alive) {
      const started = await startContainer(env);
      if (started.status === 402) {
        return jsonError('云端免费运行时长已用完（402），请到下个计费周期再试', 503);
      }
      const deadline = Date.now() + WAIT_SECONDS * 1000;
      while (Date.now() < deadline) {
        await new Promise((r) => setTimeout(r, POLL_INTERVAL_MS));
        if (await probe(backend)) { alive = true; break; }
      }
    }

    if (!alive) {
      return jsonError(
        `后端正在启动中（已触发唤醒，等待 ${WAIT_SECONDS} 秒未就绪）。请稍后重试，这次请求已经把容器拉起来了。`,
        503,
      );
    }

    // 转发原请求（保留 header，包括服务自己的 Authorization / X-Api-Token）
    const target = backend + url.pathname + url.search;
    const init = { method: request.method, headers: new Headers(request.headers) };
    init.headers.set('Host', new URL(backend).host);
    if (!['GET', 'HEAD'].includes(request.method)) {
      init.body = await request.arrayBuffer();
    }

    try {
      const resp = await fetch(target, init);
      const out = new Response(resp.body, resp);
      out.headers.set('Access-Control-Allow-Origin', '*');
      return out;
    } catch (e) {
      return jsonError(`转发失败：${String(e)}`, 502);
    }
  },
};
