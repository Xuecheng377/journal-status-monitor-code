const GITHUB_TIMEOUT_MS = 20000;

// Requests without a deadline can hang forever: the caller gives up, but the
// server keeps a socket open and the UI spins until it is restarted.
function githubFetch(url, options = {}) {
  const { timeoutMs = GITHUB_TIMEOUT_MS, ...rest } = options;
  const signal = rest.signal ?? AbortSignal.timeout(timeoutMs);
  return fetch(url, { ...rest, signal }).catch((error) => {
    if (error?.name === "TimeoutError" || error?.name === "AbortError") {
      const timeoutError = new Error(
        `请求 GitHub 超时（${Math.round(timeoutMs / 1000)} 秒），请检查网络后重试。`
      );
      timeoutError.status = 504;
      throw timeoutError;
    }
    throw error;
  });
}

function repoApiBase(owner, repo) {
  return `https://api.github.com/repos/${encodeURIComponent(owner)}/${encodeURIComponent(repo)}`;
}

function githubHeaders(token) {
  return {
    Accept: "application/vnd.github+json",
    Authorization: `Bearer ${token}`,
    "Content-Type": "application/json",
    "User-Agent": "journal-status-local-config-ui",
    "X-GitHub-Api-Version": "2022-11-28",
  };
}

async function readJsonResponse(response, fallbackMessage) {
  const text = await response.text();
  let body = null;
  if (text) {
    try {
      body = JSON.parse(text);
    } catch {
      body = { message: text };
    }
  }
  if (!response.ok) {
    throw new Error(body?.message || fallbackMessage || `GitHub request failed with HTTP ${response.status}`);
  }
  return body;
}

async function encryptSecret(value, publicKey, sodiumModule) {
  const sodium = sodiumModule || require("libsodium-wrappers");
  await sodium.ready;
  const variants = [
    sodium.base64_variants?.ORIGINAL,
    undefined,
    sodium.base64_variants?.ORIGINAL_NO_PADDING,
  ];
  let keyBytes;
  let lastError;
  for (const variant of variants) {
    try {
      keyBytes = variant === undefined ? sodium.from_base64(publicKey) : sodium.from_base64(publicKey, variant);
      break;
    } catch (error) {
      lastError = error;
    }
  }
  if (!keyBytes) {
    throw lastError || new Error("Could not decode GitHub public key.");
  }
  const valueBytes = sodium.from_string(String(value));
  const encryptedBytes = sodium.crypto_box_seal(valueBytes, keyBytes);
  return sodium.to_base64(encryptedBytes, sodium.base64_variants?.ORIGINAL);
}

async function getRepoPublicKey({ owner, repo, token, fetchImpl = githubFetch }) {
  const response = await fetchImpl(`${repoApiBase(owner, repo)}/actions/secrets/public-key`, {
    headers: githubHeaders(token),
  });
  return readJsonResponse(response, "Could not fetch repository public key.");
}

async function putRepoSecret({ owner, repo, token, name, value, fetchImpl = githubFetch, sodiumModule }) {
  const publicKey = await getRepoPublicKey({ owner, repo, token, fetchImpl });
  const encryptedValue = await encryptSecret(value, publicKey.key, sodiumModule);
  const response = await fetchImpl(`${repoApiBase(owner, repo)}/actions/secrets/${encodeURIComponent(name)}`, {
    method: "PUT",
    headers: githubHeaders(token),
    body: JSON.stringify({
      encrypted_value: encryptedValue,
      key_id: publicKey.key_id,
    }),
  });
  await readJsonResponse(response, `Could not update secret ${name}.`);
  return { name, status: "updated" };
}

async function deleteRepoSecret({ owner, repo, token, name, fetchImpl = githubFetch, ignoreNotFound = true }) {
  const response = await fetchImpl(`${repoApiBase(owner, repo)}/actions/secrets/${encodeURIComponent(name)}`, {
    method: "DELETE",
    headers: githubHeaders(token),
  });
  if (response.status === 204 || (ignoreNotFound && response.status === 404)) {
    return { name, status: response.status === 404 ? "missing" : "deleted" };
  }
  await readJsonResponse(response, `Could not delete secret ${name}.`);
  return { name, status: "deleted" };
}

async function listRepoSecrets({ owner, repo, token, fetchImpl = githubFetch }) {
  const response = await fetchImpl(`${repoApiBase(owner, repo)}/actions/secrets?per_page=100`, {
    headers: githubHeaders(token),
  });
  const body = await readJsonResponse(response, "Could not list repository secrets.");
  return Object.fromEntries((body.secrets || []).map((secret) => [secret.name, true]));
}

async function dispatchWorkflow({ owner, repo, token, workflow = "monitor.yml", ref = "main", mode = "test", fetchImpl = githubFetch }) {
  const response = await fetchImpl(`${repoApiBase(owner, repo)}/actions/workflows/${workflow}/dispatches`, {
    method: "POST",
    headers: githubHeaders(token),
    body: JSON.stringify({
      ref,
      inputs: {
        mode,
        window: "manual",
      },
    }),
  });
  if (response.status === 204) {
    return { status: "dispatched", mode };
  }
  await readJsonResponse(response, "Could not dispatch workflow.");
  return { status: "dispatched", mode };
}

async function listWorkflowRuns({ owner, repo, token, workflow = "monitor.yml", perPage = 20, fetchImpl = githubFetch, timeoutMs }) {
  const response = await fetchImpl(
    `${repoApiBase(owner, repo)}/actions/workflows/${encodeURIComponent(workflow)}/runs?per_page=${perPage}`,
    { headers: githubHeaders(token), timeoutMs }
  );
  const body = await readJsonResponse(response, "Could not list workflow runs.");
  return (body.workflow_runs || []).map((run) => ({
    id: run.id,
    number: run.run_number,
    title: run.display_title,
    event: run.event,
    status: run.status,
    conclusion: run.conclusion,
    createdAt: run.created_at,
    updatedAt: run.updated_at,
    url: run.html_url,
  }));
}

async function getRepoFile({ owner, repo, token, path: filePath, ref = "main", fetchImpl = githubFetch, timeoutMs }) {
  const response = await fetchImpl(
    `${repoApiBase(owner, repo)}/contents/${encodeURIComponent(filePath).replace(/%2F/g, "/")}?ref=${encodeURIComponent(ref)}`,
    { headers: githubHeaders(token), timeoutMs }
  );
  if (response.status === 404) {
    // A missing repository (no access, wrong name) also answers 404, and
    // treating that as "the file is empty" showed 0 manuscripts without any
    // warning. Ask the repository endpoint to tell the two apart.
    const repoResponse = await fetchImpl(repoApiBase(owner, repo), { headers: githubHeaders(token), timeoutMs });
    if (repoResponse.status === 404) {
      const error = new Error(
        `无法访问 ${owner}/${repo}：仓库不存在，或这个 token 没有被授权访问它。`
      );
      error.status = 404;
      throw error;
    }
    return { exists: false, content: null, sha: null };
  }
  const body = await readJsonResponse(response, `Could not read ${filePath}.`);
  const content = Buffer.from(body.content || "", "base64").toString("utf8");
  return { exists: true, content, sha: body.sha };
}

async function putRepoFile({ owner, repo, token, path: filePath, content, sha, message, branch = "main", fetchImpl = githubFetch }) {
  const response = await fetchImpl(
    `${repoApiBase(owner, repo)}/contents/${encodeURIComponent(filePath).replace(/%2F/g, "/")}`,
    {
      method: "PUT",
      headers: githubHeaders(token),
      body: JSON.stringify({
        message,
        content: Buffer.from(content, "utf8").toString("base64"),
        sha,
        branch,
      }),
    }
  );
  const body = await readJsonResponse(response, `Could not update ${filePath}.`);
  return { sha: body.content?.sha, commit: body.commit?.sha };
}

module.exports = {
  deleteRepoSecret,
  dispatchWorkflow,
  encryptSecret,
  getRepoFile,
  getRepoPublicKey,
  githubHeaders,
  listRepoSecrets,
  listWorkflowRuns,
  putRepoFile,
  putRepoSecret,
  repoApiBase,
};
