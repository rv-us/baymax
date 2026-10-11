// Plans: the workout plans in workout/plans, read through the same loader the robot uses.
import { useEffect, useState } from "preact/hooks";
import { api, attempt, words } from "./api";
import { Icon } from "./icons";
import { go } from "./route";

function demoText(demo: any): string {
  if (demo.keyframes) return `Custom animation, ${demo.keyframes.length} keyframes`;
  if (demo.arm) return `Both arms ${demo.arm}${demo.times > 1 ? `, ${demo.times} times` : ""}`;
  return `Built-in move “${demo.action_group}”`;
}

export function Plans() {
  const [plans, setPlans] = useState<any[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const load = () => api("GET", "/api/plans").then(setPlans).catch((e) => setError(e.message));
  useEffect(() => { load(); }, []);

  if (error) return <div class="page-empty"><Icon name="alert" size={28} /><h2>Couldn't read the plans</h2><p>{error}</p></div>;
  if (!plans) return <div class="page-empty"><p>Reading the plans…</p></div>;
  if (!plans.length) {
    return (
      <div class="page-empty"><Icon name="plans" size={28} /><h2>No workout plans yet</h2>
        <p>A plan is a list of exercises. Make an animation in Studio and add it to a plan from there.</p>
        <button class="btn primary" onClick={() => go("/")}>Open Studio</button></div>
    );
  }
  const drop = (plan: any, name: string) => {
    if (plan.exercises.length === 1) return alert("A plan needs at least one exercise. Add another before removing this one.");
    if (!confirm(`Remove “${name}” from “${plan.name}”?`)) return;
    attempt(async () => {
      await api("PUT", `/api/plans/${plan.id}`, { name: plan.name, exercises: plan.exercises.filter((e: any) => e.name !== name) });
      await load();
    }, "Removed.");
  };
  return (
    <div class="plans">
      <header class="page-head"><h1>Plans</h1><p>What the robot will teach, in order. Add exercises from Studio.</p></header>
      {plans.map((p) => (
        <section class="panel" key={p.id}>
          <div class="panel-head">
            <h2>{p.name}</h2><span class="hint">workout/plans/{p.id}.yaml</span>
          </div>
          {p.error ? <p class="note bad"><Icon name="alert" size={16} />This plan has a mistake and can't be run: {p.error}</p> : (
            <ol class="exercise-list">
              {p.exercises.map((e: any) => {
                const t = e.targets?.[e.track_joint];
                return (
                  <li key={e.name}>
                    <div>
                      <h3>{e.name}</h3>
                      <p class="cue">“{e.cue}”</p>
                      <p class="hint">
                        {e.sets} sets of {e.reps} reps, about {e.tempo_s} s each. {demoText(e.demo)}.
                        {" "}Watching {words(e.track_joint)}{t?.top != null && ` from ${t.bottom ?? 0}° to ${t.top}°`}.
                      </p>
                    </div>
                    <button class="btn icon-only quiet" aria-label={`Remove ${e.name}`} onClick={() => drop(p, e.name)}><Icon name="trash" size={18} /></button>
                  </li>
                );
              })}
            </ol>
          )}
        </section>
      ))}
    </div>
  );
}
