/* Minimal iwpriv for the TV's MediaTek mt7662u_sta driver: stat | show <name> | set <name=value>. */
#include <net/if.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/socket.h>
#include <unistd.h>

#define SIOCIWFIRSTPRIV 0x8BE0
#define RTPRIV_IOCTL_SET (SIOCIWFIRSTPRIV + 0x02)
#define RTPRIV_IOCTL_STATISTICS (SIOCIWFIRSTPRIV + 0x09)
#define RTPRIV_IOCTL_SHOW (SIOCIWFIRSTPRIV + 0x11)

struct iw_point_ { void *pointer; unsigned short length; unsigned short flags; };
struct iwreq_ { char name[IFNAMSIZ]; union { struct iw_point_ data; char pad[16]; } u; };

int main(int argc, char **argv) {
    if (argc < 3) { fprintf(stderr, "usage: wlpriv IFACE stat | show NAME | set NAME=VALUE\n"); return 2; }
    static char buffer[16384];
    struct iwreq_ request;
    memset(&request, 0, sizeof(request));
    strncpy(request.name, argv[1], IFNAMSIZ-1);
    unsigned long command;
    if (!strcmp(argv[2], "stat")) command = RTPRIV_IOCTL_STATISTICS;
    else if (argc > 3 && !strcmp(argv[2], "show")) { command = RTPRIV_IOCTL_SHOW; strncpy(buffer, argv[3], sizeof(buffer)-1); }
    else if (argc > 3 && !strcmp(argv[2], "set")) { command = RTPRIV_IOCTL_SET; strncpy(buffer, argv[3], sizeof(buffer)-1); }
    else return 2;
    request.u.data.pointer = buffer;
    request.u.data.length = command == RTPRIV_IOCTL_STATISTICS ? sizeof(buffer) : strlen(buffer)+1;
    int s = socket(AF_INET, SOCK_DGRAM, 0);
    if (s < 0 || ioctl(s, command, &request) < 0) { perror("ioctl"); return 1; }
    if (command != RTPRIV_IOCTL_SET) fwrite(buffer, 1, strnlen(buffer, sizeof(buffer)), stdout);
    putchar('\n');
    return 0;
}
