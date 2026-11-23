import type { Section } from "../sections";

export function ComingSoon({ section }: { section: Section }) {
  return (
    <div className="coming-soon">
      <h1>{section.title}</h1>
      <p>Arrives in module {section.module} of the terminal build.</p>
    </div>
  );
}
