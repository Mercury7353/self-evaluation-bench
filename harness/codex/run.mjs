// Official SDK drives the experimental researcher; Discord never uses this.
import { Codex } from '@openai/codex-sdk';
import fs from 'node:fs';
import { Outcome } from './outcome.mjs';

const config = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const codex = new Codex({
  codexPathOverride: config.wrapper,
  env: { PATH: '/usr/local/bin:/usr/bin:/bin', SEB_CODEX_LAUNCH: process.argv[2] },
  config: {
    model_provider: 'seb',
    model_providers: { seb: {
      name: 'SEB metered Responses', base_url: config.baseUrl,
      wire_api: 'responses', env_key: 'SEB_DESIGNER_TOKEN',
      request_max_retries: 0, stream_max_retries: 0,
    } },
    features: { multi_agent: false },
    agents: { max_threads: 1 },
  },
});
const options = {
  model: config.model, modelReasoningEffort: config.effort,
  workingDirectory: '/workspace', skipGitRepoCheck: true,
  approvalPolicy: 'never', sandboxMode: 'danger-full-access',
  networkAccessEnabled: true, webSearchMode: 'disabled',
};
const thread = config.resumeThread ? codex.resumeThread(config.resumeThread, options) : codex.startThread(options);
const outcome = new Outcome();
try {
  const { events } = await thread.runStreamed(fs.readFileSync(config.promptFile, 'utf8'));
  for await (const event of events) {
    process.stdout.write(JSON.stringify(event) + '\n');
    if (event.type === 'thread.started') fs.writeFileSync(config.threadFile, JSON.stringify({ thread_id: event.thread_id }) + '\n');
    outcome.observe(event);
  }
} catch (error) {
  process.stderr.write(String(error) + '\n');
  outcome.failed = true;
}
process.exitCode = outcome.exitCode;
