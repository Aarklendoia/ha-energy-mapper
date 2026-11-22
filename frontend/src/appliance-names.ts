// Frequent appliance names by power, offered as quick picks when labelling a
// live event (#17): "Toaster" for +850 W, "Kettle" for +2 kW. A small static
// list per language, unit tested.

/** Upper bound of each band (W, exclusive) and its names, most frequent first. */
const BANDS: Record<string, Array<[number, string[]]>> = {
  en: [
    [500, ["Television", "Computer", "Pool pump", "Dehumidifier", "Fridge"]],
    [1000, ["Toaster", "Microwave", "Coffee machine", "Vacuum cleaner", "Hair dryer"]],
    [1600, ["Microwave", "Hair dryer", "Iron", "Coffee machine", "Electric heater"]],
    [2400, ["Kettle", "Oven", "Water heater", "Radiator", "Hob"]],
    [3500, ["Oven", "Hob", "Water heater", "Washing machine", "Dishwasher"]],
    [Infinity, ["Hob", "Heat pump", "EV charger", "Water heater", "Sauna"]],
  ],
  fr: [
    [500, ["Télévision", "Ordinateur", "Pompe de piscine", "Déshumidificateur", "Réfrigérateur"]],
    [1000, ["Grille-pain", "Micro-ondes", "Cafetière", "Aspirateur", "Sèche-cheveux"]],
    [1600, ["Micro-ondes", "Sèche-cheveux", "Fer à repasser", "Cafetière", "Chauffage d'appoint"]],
    [2400, ["Bouilloire", "Four", "Chauffe-eau", "Radiateur", "Plaque de cuisson"]],
    [3500, ["Four", "Plaque de cuisson", "Chauffe-eau", "Lave-linge", "Lave-vaisselle"]],
    [Infinity, ["Plaque de cuisson", "Pompe à chaleur", "Borne de recharge", "Chauffe-eau", "Sauna"]],
  ],
};

/** Up to ``limit`` frequent names for a step of ``power_w`` (W, its size),
 * in the user's language (English otherwise), without the names already
 * taken (``exclude``, compared case-insensitively). */
export function frequentNames(
  power_w: number,
  language: string | undefined,
  exclude: Iterable<string> = [],
  limit = 4,
): string[] {
  const base = (language ?? "en").toLowerCase().split(/[-_]/)[0];
  const bands = BANDS[base] ?? BANDS.en;
  const p = Math.abs(power_w);
  const names = bands.find(([hi]) => p < hi)?.[1] ?? [];
  const taken = new Set([...exclude].map((n) => n.trim().toLowerCase()));
  return names.filter((n) => !taken.has(n.toLowerCase())).slice(0, limit);
}
