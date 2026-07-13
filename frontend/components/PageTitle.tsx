export function PageTitle({ title, subtitle, tags = [] }: { title: string; subtitle: string; tags?: string[] }) {
  return (
    <section className="mb-5">
      <div className="flex flex-col justify-between gap-4 md:flex-row md:items-end">
        <div>
          <h1 className="text-2xl font-semibold text-text-primary md:text-[28px] md:leading-9">{title}</h1>
          <p className="mt-2 max-w-3xl text-sm leading-6 text-text-secondary">{subtitle}</p>
        </div>
        <div className="flex flex-wrap gap-2">
          {tags.map((tag) => (
            <span className="chip" key={tag}>
              {tag}
            </span>
          ))}
        </div>
      </div>
    </section>
  );
}
