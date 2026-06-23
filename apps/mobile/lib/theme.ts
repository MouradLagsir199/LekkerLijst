export const colors = {
  background: "#ffffff",
  surface: "#ffffff",
  text: "#050505",
  muted: "#5f6368",
  border: "#e5e7eb",
  primary: "#78c883",
  primaryDark: "#65b772",
  primarySoft: "#e2f4e5",
  tabActive: "#dff2e2",
  danger: "#b91c1c",
  shadow: "#000000"
};

export const spacing = {
  xs: 6,
  sm: 10,
  md: 16,
  lg: 24,
  xl: 32
};

export const radii = {
  sm: 8,
  md: 14,
  lg: 18,
  xl: 24,
  pill: 999
};

export const typography = {
  title: {
    fontSize: 32,
    lineHeight: 38,
    fontWeight: "800" as const
  },
  sectionTitle: {
    fontSize: 26,
    lineHeight: 32,
    fontWeight: "800" as const
  },
  cardTitle: {
    fontSize: 20,
    lineHeight: 25,
    fontWeight: "800" as const
  },
  body: {
    fontSize: 16,
    lineHeight: 23,
    fontWeight: "400" as const
  },
  label: {
    fontSize: 14,
    lineHeight: 18,
    fontWeight: "600" as const
  }
};

export const shadows = {
  card: {
    shadowColor: colors.shadow,
    shadowOffset: { width: 0, height: 8 },
    shadowOpacity: 0.14,
    shadowRadius: 18,
    elevation: 7
  },
  soft: {
    shadowColor: colors.shadow,
    shadowOffset: { width: 0, height: 5 },
    shadowOpacity: 0.1,
    shadowRadius: 14,
    elevation: 4
  }
};
