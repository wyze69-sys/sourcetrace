// Regression fixture for the tree-sitter 0.26.0 native access-violation crash
// (exit 0xC0000005 in the js_parser_worker subprocess, TRACE-000).
// Eight renamed copies of a real-world crashing function shape; the crash
// requires enough allocation churn, so do not shrink this file.

function calculateXpBreakdown0(workout, options = {}) {
  const slug = getCategorySlug(workout);
  const profile = resolveCategoryProfile(slug, options.categoryMeta);
  const durationMin = getDurationMinutes(workout);
  const distanceKm = getDistanceKm(workout);
  const defaultMet = Number(
    options.defaultMet ?? options.categoryMeta?.baseMet ?? options.categoryMeta?.base_met ?? profile.baseMet
  );
  const weeklyStreak = Number(options.weeklyStreak || 0);

  // Adjust defaultMet by intensity
  const intensity = workout.intensity || options.intensity || "med";
  const intensityMult = getIntensityMultiplier(intensity);
  const adjustedMet = defaultMet * intensityMult;

  const baseCompletionXp = 20;
  const durationXp = Math.min(durationMin * 1.2, 90);
  const intensityXp = Math.min(adjustedMet * durationMin * 0.15, 60);

  const cardioBonus = Math.min(distanceKm * 4, 40);

  const totalVolumeKg = Number(options.totalVolumeKg || 0);
  const strengthBonus = Math.min(totalVolumeKg / 500, 40);

  const bodyweightFactorRaw = options.bodyweightFactor;
  const hasBodyweightFactor = bodyweightFactorRaw !== undefined && bodyweightFactorRaw !== null;
  const bodyweightFactor = Number(bodyweightFactorRaw || 0);
  const bodyweightReps = Number(options.reps || 0);
  const bodyweightBonus = hasBodyweightFactor
    ? Math.min(bodyweightReps * bodyweightFactor * 0.25, 40)
    : 0;

  const performanceBonus = Math.max(cardioBonus, strengthBonus, bodyweightBonus, 0);
  const streakBonus = streakBonusForWeeklyStreak0(weeklyStreak);

  const finalXp = Math.round(
    baseCompletionXp + durationXp + intensityXp + performanceBonus + streakBonus
  );

  const breakdown = {
    baseCompletionXp,
    durationXp,
    intensityXp,
    cardioBonus,
    strengthBonus,
    bodyweightBonus,
    performanceBonus,
    streakBonus,
    finalXp,
    met: defaultMet,
    durationMin,
    weeklyStreak,
    formulaVersion: FORMULA_VERSION
  };
  if (distanceKm > 0) breakdown.distanceKm = distanceKm;
  if (totalVolumeKg > 0) breakdown.totalVolumeKg = totalVolumeKg;
  if (hasBodyweightFactor) breakdown.bodyweightFactor = bodyweightFactor;

  return breakdown;
}

function calculateXpBreakdown1(workout, options = {}) {
  const slug = getCategorySlug(workout);
  const profile = resolveCategoryProfile(slug, options.categoryMeta);
  const durationMin = getDurationMinutes(workout);
  const distanceKm = getDistanceKm(workout);
  const defaultMet = Number(
    options.defaultMet ?? options.categoryMeta?.baseMet ?? options.categoryMeta?.base_met ?? profile.baseMet
  );
  const weeklyStreak = Number(options.weeklyStreak || 0);

  // Adjust defaultMet by intensity
  const intensity = workout.intensity || options.intensity || "med";
  const intensityMult = getIntensityMultiplier(intensity);
  const adjustedMet = defaultMet * intensityMult;

  const baseCompletionXp = 20;
  const durationXp = Math.min(durationMin * 1.2, 90);
  const intensityXp = Math.min(adjustedMet * durationMin * 0.15, 60);

  const cardioBonus = Math.min(distanceKm * 4, 40);

  const totalVolumeKg = Number(options.totalVolumeKg || 0);
  const strengthBonus = Math.min(totalVolumeKg / 500, 40);

  const bodyweightFactorRaw = options.bodyweightFactor;
  const hasBodyweightFactor = bodyweightFactorRaw !== undefined && bodyweightFactorRaw !== null;
  const bodyweightFactor = Number(bodyweightFactorRaw || 0);
  const bodyweightReps = Number(options.reps || 0);
  const bodyweightBonus = hasBodyweightFactor
    ? Math.min(bodyweightReps * bodyweightFactor * 0.25, 40)
    : 0;

  const performanceBonus = Math.max(cardioBonus, strengthBonus, bodyweightBonus, 0);
  const streakBonus = streakBonusForWeeklyStreak1(weeklyStreak);

  const finalXp = Math.round(
    baseCompletionXp + durationXp + intensityXp + performanceBonus + streakBonus
  );

  const breakdown = {
    baseCompletionXp,
    durationXp,
    intensityXp,
    cardioBonus,
    strengthBonus,
    bodyweightBonus,
    performanceBonus,
    streakBonus,
    finalXp,
    met: defaultMet,
    durationMin,
    weeklyStreak,
    formulaVersion: FORMULA_VERSION
  };
  if (distanceKm > 0) breakdown.distanceKm = distanceKm;
  if (totalVolumeKg > 0) breakdown.totalVolumeKg = totalVolumeKg;
  if (hasBodyweightFactor) breakdown.bodyweightFactor = bodyweightFactor;

  return breakdown;
}

function calculateXpBreakdown2(workout, options = {}) {
  const slug = getCategorySlug(workout);
  const profile = resolveCategoryProfile(slug, options.categoryMeta);
  const durationMin = getDurationMinutes(workout);
  const distanceKm = getDistanceKm(workout);
  const defaultMet = Number(
    options.defaultMet ?? options.categoryMeta?.baseMet ?? options.categoryMeta?.base_met ?? profile.baseMet
  );
  const weeklyStreak = Number(options.weeklyStreak || 0);

  // Adjust defaultMet by intensity
  const intensity = workout.intensity || options.intensity || "med";
  const intensityMult = getIntensityMultiplier(intensity);
  const adjustedMet = defaultMet * intensityMult;

  const baseCompletionXp = 20;
  const durationXp = Math.min(durationMin * 1.2, 90);
  const intensityXp = Math.min(adjustedMet * durationMin * 0.15, 60);

  const cardioBonus = Math.min(distanceKm * 4, 40);

  const totalVolumeKg = Number(options.totalVolumeKg || 0);
  const strengthBonus = Math.min(totalVolumeKg / 500, 40);

  const bodyweightFactorRaw = options.bodyweightFactor;
  const hasBodyweightFactor = bodyweightFactorRaw !== undefined && bodyweightFactorRaw !== null;
  const bodyweightFactor = Number(bodyweightFactorRaw || 0);
  const bodyweightReps = Number(options.reps || 0);
  const bodyweightBonus = hasBodyweightFactor
    ? Math.min(bodyweightReps * bodyweightFactor * 0.25, 40)
    : 0;

  const performanceBonus = Math.max(cardioBonus, strengthBonus, bodyweightBonus, 0);
  const streakBonus = streakBonusForWeeklyStreak2(weeklyStreak);

  const finalXp = Math.round(
    baseCompletionXp + durationXp + intensityXp + performanceBonus + streakBonus
  );

  const breakdown = {
    baseCompletionXp,
    durationXp,
    intensityXp,
    cardioBonus,
    strengthBonus,
    bodyweightBonus,
    performanceBonus,
    streakBonus,
    finalXp,
    met: defaultMet,
    durationMin,
    weeklyStreak,
    formulaVersion: FORMULA_VERSION
  };
  if (distanceKm > 0) breakdown.distanceKm = distanceKm;
  if (totalVolumeKg > 0) breakdown.totalVolumeKg = totalVolumeKg;
  if (hasBodyweightFactor) breakdown.bodyweightFactor = bodyweightFactor;

  return breakdown;
}

function calculateXpBreakdown3(workout, options = {}) {
  const slug = getCategorySlug(workout);
  const profile = resolveCategoryProfile(slug, options.categoryMeta);
  const durationMin = getDurationMinutes(workout);
  const distanceKm = getDistanceKm(workout);
  const defaultMet = Number(
    options.defaultMet ?? options.categoryMeta?.baseMet ?? options.categoryMeta?.base_met ?? profile.baseMet
  );
  const weeklyStreak = Number(options.weeklyStreak || 0);

  // Adjust defaultMet by intensity
  const intensity = workout.intensity || options.intensity || "med";
  const intensityMult = getIntensityMultiplier(intensity);
  const adjustedMet = defaultMet * intensityMult;

  const baseCompletionXp = 20;
  const durationXp = Math.min(durationMin * 1.2, 90);
  const intensityXp = Math.min(adjustedMet * durationMin * 0.15, 60);

  const cardioBonus = Math.min(distanceKm * 4, 40);

  const totalVolumeKg = Number(options.totalVolumeKg || 0);
  const strengthBonus = Math.min(totalVolumeKg / 500, 40);

  const bodyweightFactorRaw = options.bodyweightFactor;
  const hasBodyweightFactor = bodyweightFactorRaw !== undefined && bodyweightFactorRaw !== null;
  const bodyweightFactor = Number(bodyweightFactorRaw || 0);
  const bodyweightReps = Number(options.reps || 0);
  const bodyweightBonus = hasBodyweightFactor
    ? Math.min(bodyweightReps * bodyweightFactor * 0.25, 40)
    : 0;

  const performanceBonus = Math.max(cardioBonus, strengthBonus, bodyweightBonus, 0);
  const streakBonus = streakBonusForWeeklyStreak3(weeklyStreak);

  const finalXp = Math.round(
    baseCompletionXp + durationXp + intensityXp + performanceBonus + streakBonus
  );

  const breakdown = {
    baseCompletionXp,
    durationXp,
    intensityXp,
    cardioBonus,
    strengthBonus,
    bodyweightBonus,
    performanceBonus,
    streakBonus,
    finalXp,
    met: defaultMet,
    durationMin,
    weeklyStreak,
    formulaVersion: FORMULA_VERSION
  };
  if (distanceKm > 0) breakdown.distanceKm = distanceKm;
  if (totalVolumeKg > 0) breakdown.totalVolumeKg = totalVolumeKg;
  if (hasBodyweightFactor) breakdown.bodyweightFactor = bodyweightFactor;

  return breakdown;
}

function calculateXpBreakdown4(workout, options = {}) {
  const slug = getCategorySlug(workout);
  const profile = resolveCategoryProfile(slug, options.categoryMeta);
  const durationMin = getDurationMinutes(workout);
  const distanceKm = getDistanceKm(workout);
  const defaultMet = Number(
    options.defaultMet ?? options.categoryMeta?.baseMet ?? options.categoryMeta?.base_met ?? profile.baseMet
  );
  const weeklyStreak = Number(options.weeklyStreak || 0);

  // Adjust defaultMet by intensity
  const intensity = workout.intensity || options.intensity || "med";
  const intensityMult = getIntensityMultiplier(intensity);
  const adjustedMet = defaultMet * intensityMult;

  const baseCompletionXp = 20;
  const durationXp = Math.min(durationMin * 1.2, 90);
  const intensityXp = Math.min(adjustedMet * durationMin * 0.15, 60);

  const cardioBonus = Math.min(distanceKm * 4, 40);

  const totalVolumeKg = Number(options.totalVolumeKg || 0);
  const strengthBonus = Math.min(totalVolumeKg / 500, 40);

  const bodyweightFactorRaw = options.bodyweightFactor;
  const hasBodyweightFactor = bodyweightFactorRaw !== undefined && bodyweightFactorRaw !== null;
  const bodyweightFactor = Number(bodyweightFactorRaw || 0);
  const bodyweightReps = Number(options.reps || 0);
  const bodyweightBonus = hasBodyweightFactor
    ? Math.min(bodyweightReps * bodyweightFactor * 0.25, 40)
    : 0;

  const performanceBonus = Math.max(cardioBonus, strengthBonus, bodyweightBonus, 0);
  const streakBonus = streakBonusForWeeklyStreak4(weeklyStreak);

  const finalXp = Math.round(
    baseCompletionXp + durationXp + intensityXp + performanceBonus + streakBonus
  );

  const breakdown = {
    baseCompletionXp,
    durationXp,
    intensityXp,
    cardioBonus,
    strengthBonus,
    bodyweightBonus,
    performanceBonus,
    streakBonus,
    finalXp,
    met: defaultMet,
    durationMin,
    weeklyStreak,
    formulaVersion: FORMULA_VERSION
  };
  if (distanceKm > 0) breakdown.distanceKm = distanceKm;
  if (totalVolumeKg > 0) breakdown.totalVolumeKg = totalVolumeKg;
  if (hasBodyweightFactor) breakdown.bodyweightFactor = bodyweightFactor;

  return breakdown;
}

function calculateXpBreakdown5(workout, options = {}) {
  const slug = getCategorySlug(workout);
  const profile = resolveCategoryProfile(slug, options.categoryMeta);
  const durationMin = getDurationMinutes(workout);
  const distanceKm = getDistanceKm(workout);
  const defaultMet = Number(
    options.defaultMet ?? options.categoryMeta?.baseMet ?? options.categoryMeta?.base_met ?? profile.baseMet
  );
  const weeklyStreak = Number(options.weeklyStreak || 0);

  // Adjust defaultMet by intensity
  const intensity = workout.intensity || options.intensity || "med";
  const intensityMult = getIntensityMultiplier(intensity);
  const adjustedMet = defaultMet * intensityMult;

  const baseCompletionXp = 20;
  const durationXp = Math.min(durationMin * 1.2, 90);
  const intensityXp = Math.min(adjustedMet * durationMin * 0.15, 60);

  const cardioBonus = Math.min(distanceKm * 4, 40);

  const totalVolumeKg = Number(options.totalVolumeKg || 0);
  const strengthBonus = Math.min(totalVolumeKg / 500, 40);

  const bodyweightFactorRaw = options.bodyweightFactor;
  const hasBodyweightFactor = bodyweightFactorRaw !== undefined && bodyweightFactorRaw !== null;
  const bodyweightFactor = Number(bodyweightFactorRaw || 0);
  const bodyweightReps = Number(options.reps || 0);
  const bodyweightBonus = hasBodyweightFactor
    ? Math.min(bodyweightReps * bodyweightFactor * 0.25, 40)
    : 0;

  const performanceBonus = Math.max(cardioBonus, strengthBonus, bodyweightBonus, 0);
  const streakBonus = streakBonusForWeeklyStreak5(weeklyStreak);

  const finalXp = Math.round(
    baseCompletionXp + durationXp + intensityXp + performanceBonus + streakBonus
  );

  const breakdown = {
    baseCompletionXp,
    durationXp,
    intensityXp,
    cardioBonus,
    strengthBonus,
    bodyweightBonus,
    performanceBonus,
    streakBonus,
    finalXp,
    met: defaultMet,
    durationMin,
    weeklyStreak,
    formulaVersion: FORMULA_VERSION
  };
  if (distanceKm > 0) breakdown.distanceKm = distanceKm;
  if (totalVolumeKg > 0) breakdown.totalVolumeKg = totalVolumeKg;
  if (hasBodyweightFactor) breakdown.bodyweightFactor = bodyweightFactor;

  return breakdown;
}

function calculateXpBreakdown6(workout, options = {}) {
  const slug = getCategorySlug(workout);
  const profile = resolveCategoryProfile(slug, options.categoryMeta);
  const durationMin = getDurationMinutes(workout);
  const distanceKm = getDistanceKm(workout);
  const defaultMet = Number(
    options.defaultMet ?? options.categoryMeta?.baseMet ?? options.categoryMeta?.base_met ?? profile.baseMet
  );
  const weeklyStreak = Number(options.weeklyStreak || 0);

  // Adjust defaultMet by intensity
  const intensity = workout.intensity || options.intensity || "med";
  const intensityMult = getIntensityMultiplier(intensity);
  const adjustedMet = defaultMet * intensityMult;

  const baseCompletionXp = 20;
  const durationXp = Math.min(durationMin * 1.2, 90);
  const intensityXp = Math.min(adjustedMet * durationMin * 0.15, 60);

  const cardioBonus = Math.min(distanceKm * 4, 40);

  const totalVolumeKg = Number(options.totalVolumeKg || 0);
  const strengthBonus = Math.min(totalVolumeKg / 500, 40);

  const bodyweightFactorRaw = options.bodyweightFactor;
  const hasBodyweightFactor = bodyweightFactorRaw !== undefined && bodyweightFactorRaw !== null;
  const bodyweightFactor = Number(bodyweightFactorRaw || 0);
  const bodyweightReps = Number(options.reps || 0);
  const bodyweightBonus = hasBodyweightFactor
    ? Math.min(bodyweightReps * bodyweightFactor * 0.25, 40)
    : 0;

  const performanceBonus = Math.max(cardioBonus, strengthBonus, bodyweightBonus, 0);
  const streakBonus = streakBonusForWeeklyStreak6(weeklyStreak);

  const finalXp = Math.round(
    baseCompletionXp + durationXp + intensityXp + performanceBonus + streakBonus
  );

  const breakdown = {
    baseCompletionXp,
    durationXp,
    intensityXp,
    cardioBonus,
    strengthBonus,
    bodyweightBonus,
    performanceBonus,
    streakBonus,
    finalXp,
    met: defaultMet,
    durationMin,
    weeklyStreak,
    formulaVersion: FORMULA_VERSION
  };
  if (distanceKm > 0) breakdown.distanceKm = distanceKm;
  if (totalVolumeKg > 0) breakdown.totalVolumeKg = totalVolumeKg;
  if (hasBodyweightFactor) breakdown.bodyweightFactor = bodyweightFactor;

  return breakdown;
}

function calculateXpBreakdown7(workout, options = {}) {
  const slug = getCategorySlug(workout);
  const profile = resolveCategoryProfile(slug, options.categoryMeta);
  const durationMin = getDurationMinutes(workout);
  const distanceKm = getDistanceKm(workout);
  const defaultMet = Number(
    options.defaultMet ?? options.categoryMeta?.baseMet ?? options.categoryMeta?.base_met ?? profile.baseMet
  );
  const weeklyStreak = Number(options.weeklyStreak || 0);

  // Adjust defaultMet by intensity
  const intensity = workout.intensity || options.intensity || "med";
  const intensityMult = getIntensityMultiplier(intensity);
  const adjustedMet = defaultMet * intensityMult;

  const baseCompletionXp = 20;
  const durationXp = Math.min(durationMin * 1.2, 90);
  const intensityXp = Math.min(adjustedMet * durationMin * 0.15, 60);

  const cardioBonus = Math.min(distanceKm * 4, 40);

  const totalVolumeKg = Number(options.totalVolumeKg || 0);
  const strengthBonus = Math.min(totalVolumeKg / 500, 40);

  const bodyweightFactorRaw = options.bodyweightFactor;
  const hasBodyweightFactor = bodyweightFactorRaw !== undefined && bodyweightFactorRaw !== null;
  const bodyweightFactor = Number(bodyweightFactorRaw || 0);
  const bodyweightReps = Number(options.reps || 0);
  const bodyweightBonus = hasBodyweightFactor
    ? Math.min(bodyweightReps * bodyweightFactor * 0.25, 40)
    : 0;

  const performanceBonus = Math.max(cardioBonus, strengthBonus, bodyweightBonus, 0);
  const streakBonus = streakBonusForWeeklyStreak7(weeklyStreak);

  const finalXp = Math.round(
    baseCompletionXp + durationXp + intensityXp + performanceBonus + streakBonus
  );

  const breakdown = {
    baseCompletionXp,
    durationXp,
    intensityXp,
    cardioBonus,
    strengthBonus,
    bodyweightBonus,
    performanceBonus,
    streakBonus,
    finalXp,
    met: defaultMet,
    durationMin,
    weeklyStreak,
    formulaVersion: FORMULA_VERSION
  };
  if (distanceKm > 0) breakdown.distanceKm = distanceKm;
  if (totalVolumeKg > 0) breakdown.totalVolumeKg = totalVolumeKg;
  if (hasBodyweightFactor) breakdown.bodyweightFactor = bodyweightFactor;

  return breakdown;
}
