// ErrorItem is explicitly non-fatal in the official SDK's ThreadItem type.
// Terminal stream errors, a failed turn, or an unfinished stream fail the run.
export class Outcome {
  completed = false;
  failed = false;
  observe(event) {
    if (event.type === 'turn.completed') this.completed = true;
    if (event.type === 'turn.failed' || event.type === 'error') this.failed = true;
  }
  get exitCode() { return this.completed && !this.failed ? 0 : 1; }
}
