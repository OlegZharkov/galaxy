export const isBetaHistoryOpen = () => {
    return !sessionStorage.getItem("useLegacyHistory");
};

export const switchToLegacyHistoryPanel = () => {
    sessionStorage.setItem("useLegacyHistory", 1);
    location.reload();
};

export const switchToBetaHistoryPanel = () => {
    sessionStorage.removeItem("useLegacyHistory");
    location.reload(false);
};
