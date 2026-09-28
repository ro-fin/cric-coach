/**
 * Every kid-facing string on the progress dashboard (US-K4).
 *
 * Centralised so the SAF kid-mode content lint (the Python suite reads this
 * file) can prove no banned coaching/medical/spin-claim phrase ever renders.
 * Keep strings number-free: the only numbers on the dashboard come from the
 * API (US-K4 data-parity).
 */

export const COPY = {
  title: "Progress",
  kidTitle: "Your wins",
  loading: "Loading your progress...",
  missingPlayer: "Add ?player=<player-id> to the address to pick a player.",
  loadFailed: "Could not load the dashboard - is the lab server running?",
  toggleToKid: "Kid mode",
  toggleToParent: "Parent view",
  trendsTitle: "Trends",
  noTrends: "No weekly report yet - trends appear after the first rollup.",
  suspectNote: "Sessions with a calibration warning are left out of these trends.",
  unqualifiedNote: "Not enough data yet for a trend claim - keep practising.",
  unqualifiedDirection: "not enough data yet",
  regressionFrame:
    "One skill dipped recently - your coach has a plan for it, so keep working together.",
  milestonesTitle: "Milestones",
  emptyMilestones: "Your first milestone is one good session away - keep going.",
  milestoneLabels: {
    personal_best: "New personal best",
    volume: "Bowling landmark",
    streak: "Practice streak",
  },
  kidPersonalBest: "You just set a new personal best - brilliant work.",
  kidVolume: "A big bowling landmark unlocked - well bowled.",
  kidStreakSuffix: "practice days in a row - what a habit.",
  workloadTitle: "Workload safety",
  noCeiling: "No overs ceiling applies for this age band.",
  bowlingDaysLabel: "Bowling days",
  noBowlingDays: "none yet",
  noWorkloadFlags: "No workload flags.",
  noWorkloadData: "No workload data yet.",
  wellnessFlag: "Pain flag active - an adult needs to review and clear it.",
  wellnessOk: "No wellness flags.",
  reportsTitle: "Reports",
  noReports: "No rollup reports yet.",
  description: "Is the practice working, and is the workload safe?",
  kidDescription: "Every milestone you have earned.",
  pickPlayerTitle: "Pick a player",
  pickPlayerAction: "Go to sessions",
  oversTile: "Overs this rolling week",
  ceilingTile: "Allowed overs",
  remainingTile: "Balls left this week",
  noRemaining: "No weekly ceiling, so no countdown.",
  oversUnit: "overs",
  ballsUnit: "balls",
  flagsTitle: "Workload flags",
  reportPeriodJoiner: "to",
  milestoneOn: "on",
} as const;
