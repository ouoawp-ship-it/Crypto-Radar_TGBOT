"use client";

import { Bar, BarChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";

export function DistributionChart({ data, valueKey = "count" }: { data: Array<Record<string, unknown>>; valueKey?: string }) {
  if (!data.length) {
    return <div className="panel p-5 text-sm text-slate-500">暂无分布数据。</div>;
  }
  return (
    <div className="panel h-72 p-4">
      <ResponsiveContainer width="100%" height="100%">
        <BarChart data={data}>
          <CartesianGrid stroke="rgba(255,255,255,0.08)" vertical={false} />
          <XAxis dataKey="label" stroke="#94a3b8" tickLine={false} axisLine={false} />
          <YAxis stroke="#94a3b8" tickLine={false} axisLine={false} />
          <Tooltip
            cursor={{ fill: "rgba(40,214,199,0.08)" }}
            contentStyle={{ background: "#0b141d", border: "1px solid rgba(255,255,255,0.12)", borderRadius: 12, color: "#e5f4ff" }}
          />
          <Bar dataKey={valueKey} fill="#28d6c7" radius={[8, 8, 0, 0]} />
        </BarChart>
      </ResponsiveContainer>
    </div>
  );
}
