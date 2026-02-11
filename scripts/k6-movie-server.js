import http from 'k6/http';
import { check, sleep } from 'k6';
import { Gauge } from 'k6/metrics';
import crypto from 'k6/crypto';
import encoding from 'k6/encoding';

const BASE_URL = __ENV.BASE_URL || 'http://localhost:3001';
const HEAD_URL = __ENV.HEAD_URL; 

const DURATION = '60s';

// Load mode:
// - "vus": constant-vus (existing behavior)
// - "rps": constant-arrival-rate (constant request rate)
const LOAD_MODE = (__ENV.LOAD_MODE || 'vus').toLowerCase();
const TARGET_RPS = __ENV.TARGET_RPS !== undefined && __ENV.TARGET_RPS !== '' ? Number(__ENV.TARGET_RPS) : undefined;
const ARRIVAL_TIME_UNIT = __ENV.ARRIVAL_TIME_UNIT || '1s';
const PRE_ALLOCATED_VUS =
  __ENV.PRE_ALLOCATED_VUS !== undefined && __ENV.PRE_ALLOCATED_VUS !== '' ? parseInt(__ENV.PRE_ALLOCATED_VUS, 10) : 20;
const MAX_VUS = __ENV.MAX_VUS !== undefined && __ENV.MAX_VUS !== '' ? parseInt(__ENV.MAX_VUS, 10) : Math.max(50, PRE_ALLOCATED_VUS * 2);

const functionalScenarios = [
  // Only include scenarios that cover our injected performance changes:
  // - /search                     (Orama limit + reranking)
  // - /top/region/:region/:count  (random-walk shuffle)
  // - /top/user/:userId/:count    (ALS loop)
  { name: 'search_movies', exec: 'searchMovies', vus: 3, duration: '300s' },
  { name: 'top_region', exec: 'topRegionRecommendations', vus: 5, duration: '300s' },
  // 3-5 VUs can be too much for the SUT for this endpoint.
  { name: 'top_user', exec: 'topUserRecommendations', vus: 5, duration: '300s' },
];

// Metrics for SUT saturation
const sutCpu = new Gauge('sut_cpu');
const sutMem = new Gauge('sut_mem');

// Calibration mode
const CALIBRATE = (__ENV.CALIBRATE || '').toLowerCase() === 'true';

// Auth
const NO_AUTH = (__ENV.NO_AUTH || '').toLowerCase() === 'true';
const AUTH_HEADER = __ENV.AUTH_HEADER || 'Authorization';
const AUTH_SCHEME = __ENV.AUTH_SCHEME || 'Bearer';
const JWT_SECRET = __ENV.JWT_SECRET || 'my-very-secret-key';
const JWT_USER_ID = __ENV.JWT_USER_ID || 'user1';
const LOG_SATURATION = (__ENV.LOG_SATURATION || 'true').toLowerCase() === 'true';

// Helper to parse duration string to seconds
function parseDuration(d) {
  const match = d.match(/(\d+)([smh])/);
  if (!match) return 60;
  const val = parseInt(match[1]);
  const unit = match[2];
  if (unit === 'm') return val * 60;
  if (unit === 'h') return val * 3600;
  return val;
}

// Build scenarios
const scenarios = {};
let totalDuration = 0;
if (CALIBRATE) {
  scenarios.calibration = {
    executor: 'ramping-vus',
    startVUs: 1,
    stages: [{ duration: '2m', target: 100 }],
    gracefulRampDown: '0s',
  };
} else {
  let startOffsetSec = 0;
  functionalScenarios.forEach((s) => {
    const duration = s.duration || DURATION;
    const durationSec = parseDuration(duration);
    totalDuration += durationSec;
    const startTime = `${startOffsetSec}s`;
    startOffsetSec += durationSec;

    const scenarioRpsEnvKey = `RPS_${s.name}`;
    const scenarioRps =
      __ENV[scenarioRpsEnvKey] !== undefined && __ENV[scenarioRpsEnvKey] !== '' ? Number(__ENV[scenarioRpsEnvKey]) : undefined;
    const effectiveRps = scenarioRps ?? s.rps ?? TARGET_RPS;

    const useRpsMode = LOAD_MODE === 'rps' || effectiveRps !== undefined;
    if (useRpsMode) {
      if (!effectiveRps || Number.isNaN(effectiveRps) || effectiveRps <= 0) {
        throw new Error(
          `RPS mode requires a positive rate. Set TARGET_RPS or ${scenarioRpsEnvKey} (scenario "${s.name}"). Received: ${effectiveRps}`
        );
      }
      scenarios[s.name] = {
        executor: 'constant-arrival-rate',
        rate: effectiveRps,
        timeUnit: ARRIVAL_TIME_UNIT,
        preAllocatedVUs: PRE_ALLOCATED_VUS,
        maxVUs: MAX_VUS,
        startTime,
        duration,
        exec: s.exec,
      };
    } else {
      scenarios[s.name] = {
        executor: 'constant-vus',
        vus: s.vus,
        startTime,
        duration,
        exec: s.exec,
      };
    }
  });

  // Monitor runs in parallel with ALL scenarios
  scenarios.monitor = {
    executor: 'constant-vus',
    vus: 1,
    startTime: '0s',
    duration: `${totalDuration}s`,
    exec: 'monitor',
  };
}

export const options = { scenarios, noVUConnectionReuse: true };

function toBase64Url(b64) {
  return b64.replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/g, '');
}

function buildJwtHS256({ userId, secret }) {
  const header = { alg: 'HS256', typ: 'JWT' };
  const payload = { userId, iat: Math.floor(Date.now() / 1000) };

  const headerB64 = toBase64Url(encoding.b64encode(JSON.stringify(header)));
  const payloadB64 = toBase64Url(encoding.b64encode(JSON.stringify(payload)));
  const signingInput = `${headerB64}.${payloadB64}`;

  const sigB64 = toBase64Url(crypto.hmac('sha256', secret, signingInput, 'base64'));
  return `${signingInput}.${sigB64}`;
}

const AUTH_TOKEN =
  __ENV.AUTH_TOKEN || (NO_AUTH ? undefined : buildJwtHS256({ userId: JWT_USER_ID, secret: JWT_SECRET }));

function logObject(obj) {
  console.log(JSON.stringify(obj));
}
  

function buildHeaders() {
  if (NO_AUTH || !AUTH_TOKEN) return {};
  return { [AUTH_HEADER]: `${AUTH_SCHEME} ${AUTH_TOKEN}` };
}

const baseUrlA = BASE_URL;
const baseUrlB = HEAD_URL;

const TARGETS = HEAD_URL
  ? [
      { name: 'base', url: BASE_URL },
      { name: 'head', url: HEAD_URL },
    ]
  : [{ name: 'base', url: BASE_URL }];

function logRequest(res, url) {
  logObject({
    time: res.timings.duration,
    url,
    status: res.status,
    timestamp: new Date().toISOString(),
  });
}

function logRequestPair(reqName, path, resultsByTarget) {
  const baseResult = resultsByTarget.base;
  const headResult = resultsByTarget.head;
  logObject({
    endpoint: reqName,
    path,
    timestamp: new Date().toISOString(),
    base: baseResult
      ? {
          time: baseResult.res.timings.duration,
          url: `${baseUrlA}${path}`,
          status: baseResult.res.status,
        }
      : null,
    head: headResult
      ? {
          time: headResult.res.timings.duration,
          url: `${baseUrlB}${path}`,
          status: headResult.res.status,
        }
      : null,
  });
}

function runRequest(reqName, method, path, body) {
  const headers = buildHeaders();
  if (TARGETS.length === 1) {
    const t = TARGETS[0];
    const requestUrl = `${t.url}${path}`;
    const res = http.request(method, requestUrl, body, {
      headers,
      tags: { system: t.name, endpoint: reqName },
    });
    logRequest(res, `${baseUrlA}${path}`);
    check(res, { [`${reqName} ok`]: (r) => r.status === 200 }, { system: t.name, endpoint: reqName });
    return;
  }

  const shuffledTargets = TARGETS.sort(() => Math.random() - 0.5);

  const responses = http.batch(
    shuffledTargets.map((t) => ({
      method,
      url: `${t.url}${path}`,
      body,
      params: { headers, tags: { system: t.name, endpoint: reqName } },
    }))
  );

  const resultsByTarget = {};
  for (let i = 0; i < responses.length; i++) {
    const t = shuffledTargets[i];
    const res = responses[i];
    resultsByTarget[t.name] = { res };
    check(res, { [`${reqName} ok`]: (r) => r.status === 200 }, { system: t.name, endpoint: reqName });
  }

  logRequestPair(reqName, path, resultsByTarget);
}

// === Endpoints and Monitor ===

export function logSaturation() {
  const sutHost = BASE_URL.split('://')[1].split(':')[0];
  const res = http.get(`http://${sutHost}/saturation`);
  if (res.status === 200) {
    let data;
    try {
      data = res.json();
    } catch (e) {
      const body = res.body.trim();
      try {
        data = JSON.parse(body);
      } catch (e2) {
        console.warn(`Failed to parse saturation JSON: ${body}`);
        return null;
      }
    }

    const cpu = data.cpu_percent;
    const mem = data.memory_percent;

    sutCpu.add(cpu);
    sutMem.add(mem);

    if (!CALIBRATE && LOG_SATURATION) {
      console.log(`SUT Saturation: CPU=${cpu}%, MEM=${mem}%`);
    }
    return cpu;
  }
  return null;
}

function sleepInRandomRange(min, max) {
  if (max < min) {
    throw new Error("max should be greater than min");
  }

  sleep(Math.random() * (max - min) + min);
}

export function monitor() {
  if (LOG_SATURATION) {
    logSaturation();
  }
  sleep(1); // Check saturation once per second to avoid OOM/resource exhaustion
}

function doSearchMovies() {
  runRequest('search', 'GET', `/search?query=the`);
}

export function searchMovies() {
  doSearchMovies();
  sleepInRandomRange(0, 1);
}

function doTopUserRecommendations() {
  const userId = JWT_USER_ID;
  const count = 5;
  runRequest('top_user', 'GET', `/top/user/${userId}/${count}`);
}

export function topUserRecommendations() {
  doTopUserRecommendations();
  sleepInRandomRange(0, 1);
}

function doTopRegionRecommendations() {
  // Use the same region as the evaluation setup by default.
  const region = 'us';
  const count = 5;
  runRequest('top_region', 'GET', `/top/region/${region}/${count}`);
}

export function topRegionRecommendations() {
  doTopRegionRecommendations();
  sleepInRandomRange(0, 1);
}

export default function () {
  // Used only for calibration mode
  if (CALIBRATE) {
    // Only calibrate on the same set of perf-relevant endpoints.
    const endpoints = [searchMovies, topUserRecommendations, topRegionRecommendations];
    endpoints[Math.floor(Math.random() * endpoints.length)]();
  }
}
