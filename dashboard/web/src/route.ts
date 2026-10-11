import { signal } from "@preact/signals";

export const route = signal(location.hash.slice(1) || "/");
window.addEventListener("hashchange", () => { route.value = location.hash.slice(1) || "/"; window.scrollTo(0, 0); });
export const go = (path: string) => { location.hash = path; };
