import 'dart:convert';
import 'dart:math' as math;
import 'package:flutter/material.dart';
import 'package:geolocator/geolocator.dart';
import 'package:http/http.dart' as http;
import 'package:http_parser/http_parser.dart';
import 'package:image_picker/image_picker.dart';
import 'package:mime/mime.dart';
import 'package:video_player/video_player.dart';

void main() => runApp(const SurveyApp());

const Color maroon = Color(0xFF5B1018);
const Color deepMaroon = Color(0xFF31090E);
const Color gold = Color(0xFFD8B15A);
const Color ivory = Color(0xFFFFFBF5);

// ============================================================
// KOMPONEN DEKORASI EMBOSS / NEUMORPHIC
// Hanya untuk tampilan (styling). Tidak menyentuh logika aplikasi.
//  - raised : permukaan timbul (kartu, tombol sekunder)
//  - inset  : permukaan cekung (kolom input, kotak informasi)
//  - primary: tombol utama gradasi maroon dengan garis emas
// ============================================================

class Emboss {
  Emboss._();

  static const Color _shadow = Color(0x40705236);
  static const Color _highlight = Color(0xB3FFFFFF);
  static const Color _goldLine = Color(0x99D8B15A);

  static BoxDecoration raised({
    double radius = 18,
    bool soft = false,
    List<Color>? colors,
    Color? borderColor,
  }) {
    return BoxDecoration(
      borderRadius: BorderRadius.circular(radius),
      gradient: LinearGradient(
        begin: Alignment.topLeft,
        end: Alignment.bottomRight,
        colors: colors ?? const [Color(0xFFFFFFFF), Color(0xFFFAF2E3)],
      ),
      border: Border.all(color: borderColor ?? _goldLine, width: 1),
      boxShadow: [
        BoxShadow(
          color: _shadow,
          offset: soft ? const Offset(3, 3) : const Offset(6, 6),
          blurRadius: soft ? 8 : 16,
        ),
        BoxShadow(
          color: _highlight,
          offset: soft ? const Offset(-2, -2) : const Offset(-4, -4),
          blurRadius: soft ? 6 : 12,
        ),
      ],
    );
  }

  static BoxDecoration inset({
    double radius = 14,
    bool focused = false,
    List<Color>? colors,
    Color? borderColor,
  }) {
    return BoxDecoration(
      borderRadius: BorderRadius.circular(radius),
      gradient: LinearGradient(
        begin: Alignment.topLeft,
        end: Alignment.bottomRight,
        colors: colors ??
            const [Color(0xFFEFE5D2), Color(0xFFF8F0E1), Color(0xFFFFFCF6)],
        stops: colors == null ? const [0.0, 0.55, 1.0] : null,
      ),
      border: Border.all(
        color: focused
            ? maroon.withValues(alpha: 0.75)
            : (borderColor ?? const Color(0x8CD8B15A)),
        width: focused ? 1.4 : 1,
      ),
      boxShadow: const [
        BoxShadow(
          color: Color(0xF2FFFFFF),
          offset: Offset(1.5, 1.5),
          blurRadius: 2,
        ),
      ],
    );
  }

  static BoxDecoration primary({double radius = 14, bool strong = false}) {
    return BoxDecoration(
      borderRadius: BorderRadius.circular(radius),
      gradient: const LinearGradient(
        begin: Alignment.topLeft,
        end: Alignment.bottomRight,
        colors: [Color(0xFF8B2531), maroon, deepMaroon],
        stops: [0.0, 0.5, 1.0],
      ),
      border: Border.all(color: const Color(0xFFD8B15A), width: 1.2),
      boxShadow: [
        BoxShadow(
          color: const Color(0x665B1018),
          offset: strong ? const Offset(5, 8) : const Offset(3, 5),
          blurRadius: strong ? 18 : 10,
        ),
        const BoxShadow(
          color: Color(0x80FFFFFF),
          offset: Offset(-2, -2),
          blurRadius: 5,
        ),
      ],
    );
  }

  /// Gaya untuk FilledButton yang dibungkus [EmbossButton] (primary: true).
  static ButtonStyle primaryStyle({double vertical = 16}) {
    return FilledButton.styleFrom(
      backgroundColor: Colors.transparent,
      disabledBackgroundColor: Colors.transparent,
      foregroundColor: Colors.white,
      disabledForegroundColor: Colors.white70,
      shadowColor: Colors.transparent,
      surfaceTintColor: Colors.transparent,
      padding: EdgeInsets.symmetric(vertical: vertical, horizontal: 20),
      shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(14)),
      textStyle: const TextStyle(fontWeight: FontWeight.bold, letterSpacing: 0.4),
    );
  }

  /// Gaya untuk OutlinedButton yang dibungkus [EmbossButton].
  static ButtonStyle secondaryStyle({double vertical = 14}) {
    return OutlinedButton.styleFrom(
      backgroundColor: Colors.transparent,
      disabledBackgroundColor: Colors.transparent,
      foregroundColor: maroon,
      shadowColor: Colors.transparent,
      surfaceTintColor: Colors.transparent,
      side: BorderSide.none,
      padding: EdgeInsets.symmetric(vertical: vertical, horizontal: 18),
      shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(14)),
      textStyle: const TextStyle(fontWeight: FontWeight.w600, letterSpacing: 0.3),
    );
  }
}

/// Kartu timbul (raised) atau cekung (pressed). Ripple anak tetap berfungsi.
class EmbossCard extends StatelessWidget {
  const EmbossCard({
    super.key,
    required this.child,
    this.padding = EdgeInsets.zero,
    this.margin = const EdgeInsets.symmetric(horizontal: 2, vertical: 6),
    this.radius = 18,
    this.pressed = false,
    this.soft = false,
    this.colors,
    this.borderColor,
  });

  final Widget child;
  final EdgeInsetsGeometry padding;
  final EdgeInsetsGeometry margin;
  final double radius;
  final bool pressed;
  final bool soft;
  final List<Color>? colors;
  final Color? borderColor;

  @override
  Widget build(BuildContext context) {
    return AnimatedContainer(
      duration: const Duration(milliseconds: 180),
      curve: Curves.easeOut,
      margin: margin,
      decoration: pressed
          ? Emboss.inset(radius: radius, colors: colors, borderColor: borderColor)
          : Emboss.raised(
              radius: radius,
              soft: soft,
              colors: colors,
              borderColor: borderColor,
            ),
      child: Material(
        type: MaterialType.transparency,
        borderRadius: BorderRadius.circular(radius),
        clipBehavior: Clip.antiAlias,
        child: Padding(padding: padding, child: child),
      ),
    );
  }
}

/// Pembungkus kolom input / dropdown agar tampak sedikit cekung.
class EmbossInput extends StatefulWidget {
  const EmbossInput({super.key, required this.child, this.radius = 14});

  final Widget child;
  final double radius;

  @override
  State<EmbossInput> createState() => _EmbossInputState();
}

class _EmbossInputState extends State<EmbossInput> {
  bool _focused = false;

  @override
  Widget build(BuildContext context) {
    return Focus(
      skipTraversal: true,
      onFocusChange: (hasFocus) {
        if (mounted && _focused != hasFocus) setState(() => _focused = hasFocus);
      },
      child: AnimatedContainer(
        duration: const Duration(milliseconds: 160),
        decoration: Emboss.inset(radius: widget.radius, focused: _focused),
        child: widget.child,
      ),
    );
  }
}

/// Pembungkus tombol: gradasi maroon + aksen emas (primary) atau timbul (sekunder).
/// Tombol asli (FilledButton/OutlinedButton) tetap menjadi child dan menjalankan fungsi aslinya.
class EmbossButton extends StatelessWidget {
  const EmbossButton({
    super.key,
    required this.child,
    this.enabled = true,
    this.primary = false,
    this.radius = 14,
  });

  final Widget child;
  final bool enabled;
  final bool primary;
  final double radius;

  @override
  Widget build(BuildContext context) {
    return Opacity(
      opacity: enabled ? 1 : 0.55,
      child: DecoratedBox(
        decoration: primary
            ? Emboss.primary(radius: radius)
            : Emboss.raised(
                radius: radius,
                soft: true,
                borderColor: const Color(0xCCD8B15A),
              ),
        child: child,
      ),
    );
  }
}

/// Medali ikon kecil untuk judul seksi.
class EmbossIconBadge extends StatelessWidget {
  const EmbossIconBadge({super.key, required this.icon});

  final IconData icon;

  @override
  Widget build(BuildContext context) {
    return Container(
      width: 34,
      height: 34,
      decoration: BoxDecoration(
        shape: BoxShape.circle,
        gradient: const LinearGradient(
          begin: Alignment.topLeft,
          end: Alignment.bottomRight,
          colors: [Color(0xFF8B2531), deepMaroon],
        ),
        border: Border.all(color: gold, width: 1.2),
        boxShadow: const [
          BoxShadow(color: Color(0x4D31090E), offset: Offset(2, 3), blurRadius: 6),
          BoxShadow(color: Color(0xB3FFFFFF), offset: Offset(-2, -2), blurRadius: 4),
        ],
      ),
      child: Icon(icon, color: gold, size: 18),
    );
  }
}

/// Garis emas tipis dengan efek ukiran.
class EmbossDivider extends StatelessWidget {
  const EmbossDivider({super.key});

  @override
  Widget build(BuildContext context) {
    return Container(
      height: 2,
      decoration: BoxDecoration(
        borderRadius: BorderRadius.circular(2),
        gradient: const LinearGradient(
          colors: [Color(0x00D8B15A), gold, Color(0x00D8B15A)],
        ),
        boxShadow: const [
          BoxShadow(color: Color(0xB3FFFFFF), offset: Offset(0, 1.5), blurRadius: 1),
        ],
      ),
    );
  }
}

class SurveyApp extends StatelessWidget {
  const SurveyApp({super.key});

  @override
  Widget build(BuildContext context) => MaterialApp(
        title: 'Survei Bantuan UMKM',
        debugShowCheckedModeBanner: false,
        theme: ThemeData(
          useMaterial3: true,
          colorScheme: ColorScheme.fromSeed(
            seedColor: maroon,
            primary: maroon,
            secondary: gold,
            surface: const Color(0xFFFFFBF5),
          ),
          scaffoldBackgroundColor: const Color(0xFFFFFBF5),
          appBarTheme: const AppBarTheme(
            backgroundColor: maroon,
            foregroundColor: Colors.white,
            elevation: 6,
            scrolledUnderElevation: 6,
            shadowColor: Color(0x99000000),
            surfaceTintColor: Colors.transparent,
            shape: Border(bottom: BorderSide(color: gold, width: 1.5)),
          ),
          // Kolom input: isian dibuat cekung oleh EmbossInput, jadi bingkai
          // bawaan dimatikan agar tidak ada garis ganda.
          inputDecorationTheme: const InputDecorationTheme(
            filled: false,
            contentPadding: EdgeInsets.symmetric(horizontal: 16, vertical: 14),
            border: InputBorder.none,
            enabledBorder: InputBorder.none,
            focusedBorder: InputBorder.none,
            disabledBorder: InputBorder.none,
            errorBorder: InputBorder.none,
            focusedErrorBorder: InputBorder.none,
            labelStyle: TextStyle(color: Color(0xFF7A5B52)),
            floatingLabelStyle: TextStyle(
              color: maroon,
              fontWeight: FontWeight.w600,
            ),
            hintStyle: TextStyle(color: Colors.black38),
          ),
          textSelectionTheme: const TextSelectionThemeData(
            cursorColor: maroon,
            selectionHandleColor: maroon,
          ),
          dividerTheme: const DividerThemeData(
            color: Color(0x66D8B15A),
            thickness: 1,
          ),
          checkboxTheme: CheckboxThemeData(
            side: const BorderSide(color: gold, width: 1.6),
            shape: RoundedRectangleBorder(
              borderRadius: BorderRadius.circular(5),
            ),
          ),
          switchTheme: SwitchThemeData(
            thumbColor: WidgetStateProperty.resolveWith(
              (states) => states.contains(WidgetState.selected)
                  ? gold
                  : const Color(0xFFF1E7D6),
            ),
            trackColor: WidgetStateProperty.resolveWith(
              (states) => states.contains(WidgetState.selected)
                  ? maroon
                  : const Color(0xFFD9CDB8),
            ),
            trackOutlineColor: WidgetStateProperty.resolveWith(
              (states) => states.contains(WidgetState.selected)
                  ? gold
                  : const Color(0xFFBFA77A),
            ),
          ),
          snackBarTheme: SnackBarThemeData(
            backgroundColor: deepMaroon,
            contentTextStyle: const TextStyle(color: Colors.white),
            behavior: SnackBarBehavior.floating,
            elevation: 8,
            shape: RoundedRectangleBorder(
              borderRadius: BorderRadius.circular(14),
              side: const BorderSide(color: gold, width: 1),
            ),
          ),
          bottomSheetTheme: const BottomSheetThemeData(
            modalBackgroundColor: ivory,
            modalElevation: 12,
            surfaceTintColor: Colors.transparent,
            shape: RoundedRectangleBorder(
              borderRadius: BorderRadius.vertical(top: Radius.circular(24)),
              side: BorderSide(color: gold, width: 1),
            ),
          ),
        ),
        home: const SplashScreen(),
      );
}

// ============================================================
// SPLASH SCREEN PREMIUM SURVEI UMKM
// Durasi 6 detik, lalu otomatis menuju halaman login.
// ============================================================

class SplashScreen extends StatefulWidget {
  const SplashScreen({super.key});

  @override
  State<SplashScreen> createState() => _SplashScreenState();
}

class _SplashScreenState extends State<SplashScreen>
    with TickerProviderStateMixin {
  late final AnimationController _controller;
  late final Animation<double> _logoOpacity;
  late final Animation<double> _logoScale;
  late final Animation<double> _textOpacity;

  bool _navigating = false;

  @override
  void initState() {
    super.initState();

    _controller = AnimationController(
      vsync: this,
      duration: const Duration(seconds: 6),
    );

    _logoOpacity = CurvedAnimation(
      parent: _controller,
      curve: const Interval(0.0, 0.12, curve: Curves.easeIn),
    );

    _logoScale = Tween<double>(
      begin: 0.72,
      end: 1.0,
    ).animate(
      CurvedAnimation(
        parent: _controller,
        curve: const Interval(0.0, 0.20, curve: Curves.easeOutBack),
      ),
    );

    _textOpacity = CurvedAnimation(
      parent: _controller,
      curve: const Interval(0.25, 0.58, curve: Curves.easeIn),
    );

    _controller.forward();

    Future.delayed(const Duration(seconds: 6), () {
      if (!mounted || _navigating) return;

      _navigating = true;

      Navigator.of(context).pushReplacement(
        PageRouteBuilder(
          pageBuilder: (context, animation, secondaryAnimation) =>
              const AuthGate(),
          transitionDuration: const Duration(milliseconds: 450),
          transitionsBuilder:
              (context, animation, secondaryAnimation, child) {
            return FadeTransition(
              opacity: animation,
              child: child,
            );
          },
        ),
      );
    });
  }

  @override
  void dispose() {
    _controller.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
            body: Stack(
        fit: StackFit.expand,
        children: [
          Image.asset(
            'assets/background pembuka.png',
            fit: BoxFit.cover,
          ),

          SafeArea(
            child: LayoutBuilder(
              builder: (context, constraints) {
                return Center(
                  child: SingleChildScrollView(
                    child: ConstrainedBox(
                      constraints: BoxConstraints(
                        minHeight: constraints.maxHeight,
                      ),
                      child: IntrinsicHeight(
                        child: Padding(
                          padding: const EdgeInsets.symmetric(
                            horizontal: 24,
                            vertical: 24,
                          ),
                          child: Column(
                            mainAxisAlignment: MainAxisAlignment.center,
                            children: [
                              const Spacer(),

                              // Logo bulat dengan api yang berputar di tepinya.
                              SizedBox(
                                width: 174,
                                height: 174,
                                child: Stack(
                                  clipBehavior: Clip.none,
                                  children: [
                                    // Api (digambar di belakang logo).
                                    Positioned.fill(
                                      child: AnimatedBuilder(
                                        animation: _controller,
                                        builder: (context, child) {
                                          return CustomPaint(
                                            painter: _WandRingPainter(
                                              progress: _controller.value,
                                              radius: 87 * _logoScale.value,
                                            ),
                                          );
                                        },
                                      ),
                                    ),

                                    // Logo asli, tanpa mengubah artwork.
                                    Positioned.fill(
                                      child: AnimatedBuilder(
                                        animation: _controller,
                                        builder: (context, child) {
                                          return Opacity(
                                            opacity: _logoOpacity.value,
                                            child: Transform.scale(
                                              scale: _logoScale.value,
                                              child: child,
                                            ),
                                          );
                                        },
                                        child: Container(
                                          padding: const EdgeInsets.all(5),
                                          decoration: BoxDecoration(
                                            shape: BoxShape.circle,
                                            color: deepMaroon,
                                            gradient: const LinearGradient(
                                              begin: Alignment.topLeft,
                                              end: Alignment.bottomRight,
                                              colors: [
                                                Color(0xFF4A111A),
                                                deepMaroon,
                                              ],
                                            ),
                                            border: Border.all(
                                              color: const Color(0xFFFFD27A),
                                              width: 2,
                                            ),
                                            boxShadow: [
                                              BoxShadow(
                                                color: const Color(0xFFFFC04D)
                                                    .withValues(alpha: 0.30),
                                                blurRadius: 28,
                                                spreadRadius: 4,
                                              ),
                                            ],
                                          ),
                                          child: ClipOval(
                                            child: Image.asset(
                                              'assets/logo.png',
                                              fit: BoxFit.contain,
                                              errorBuilder:
                                                  (context, error, stack) {
                                                return const Icon(
                                                  Icons.location_city_rounded,
                                                  color: gold,
                                                  size: 90,
                                                );
                                              },
                                            ),
                                          ),
                                        ),
                                      ),
                                    ),
                                  ],
                                ),
                              ),

                              const SizedBox(height: 48),

                              // Judul dan subjudul.
                              FadeTransition(
                                opacity: _textOpacity,
                                child: Column(
                                  children: [
                                    const Text(
                                      'APLIKASI SURVEI UMKM',
                                      textAlign: TextAlign.center,
                                      style: TextStyle(
                                        color: gold,
                                        fontSize: 25,
                                        fontWeight: FontWeight.w800,
                                        letterSpacing: 1.5,
                                        shadows: [
                                          Shadow(
                                            color: Color(0x665B1018),
                                            blurRadius: 12,
                                            offset: Offset(0, 2),
                                          ),
                                          Shadow(
                                            color: Color(0x99FFF1C2),
                                            blurRadius: 0,
                                            offset: Offset(-1, -1),
                                          ),
                                          Shadow(
                                            color: Color(0xB3000000),
                                            blurRadius: 2,
                                            offset: Offset(1.5, 1.5),
                                          ),
                                        ],
                                      ),
                                    ),

                                    const SizedBox(height: 14),

                                    Container(
                                      width: 100,
                                      height: 2,
                                      decoration: BoxDecoration(
                                        color: gold,
                                        borderRadius: BorderRadius.circular(2),
                                        boxShadow: const [
                                          BoxShadow(
                                            color: Color(0x66000000),
                                            offset: Offset(0, 1.5),
                                            blurRadius: 2,
                                          ),
                                        ],
                                      ),
                                    ),

                                    const SizedBox(height: 14),

                                    const Text(
                                      'Pendataan dan Verifikasi Bantuan UMKM',
                                      textAlign: TextAlign.center,
                                      style: TextStyle(
                                        color: Color(0xFFFFE8B0),
                                        fontSize: 14,
                                        height: 1.5,
                                        letterSpacing: 0.3,
                                      ),
                                    ),
                                  ],
                                ),
                              ),

                              const Spacer(),

                              // Indikator waktu pembuka.
                              AnimatedBuilder(
                                animation: _controller,
                                builder: (context, child) {
                                  final progress = _controller.value.clamp(0.0, 1.0);

                                  return SizedBox(
                                    width: 190,
                                    height: 14,
                                    child: Stack(
                                      alignment: Alignment.centerLeft,
                                      children: [
                                        Container(
                                          width: 190,
                                          height: 6,
                                          decoration: BoxDecoration(
                                            gradient: const LinearGradient(
                                              begin: Alignment.topCenter,
                                              end: Alignment.bottomCenter,
                                              colors: [
                                                Color(0xFF4A1A20),
                                                Color(0xFF8A5058),
                                              ],
                                            ),
                                            borderRadius: BorderRadius.circular(10),
                                            border: Border.all(
                                              color: const Color(0x55D8B15A),
                                              width: 0.8,
                                            ),
                                            boxShadow: const [
                                              BoxShadow(
                                                color: Color(0x33FFFFFF),
                                                offset: Offset(0, 1),
                                                blurRadius: 1,
                                              ),
                                            ],
                                          ),
                                        ),
                                        SizedBox(
                                          width: 190 * progress,
                                          child: Container(
                                            height: 6,
                                            decoration: BoxDecoration(
                                              gradient: const LinearGradient(
                                                colors: [
                                                  Color(0xFFB8860B),
                                                  Color(0xFFFFD700),
                                                  Color(0xFFFFF1A8),
                                                ],
                                              ),
                                              borderRadius: BorderRadius.circular(10),
                                              boxShadow: [
                                                BoxShadow(
                                                  color: gold.withValues(alpha: 0.65),
                                                  blurRadius: 6,
                                                  spreadRadius: 0.5,
                                                ),
                                              ],
                                            ),
                                          ),
                                        ),
                                        if (progress > 0.02 && progress < 0.99)
                                          Positioned(
                                            left: (182 * progress).clamp(0.0, 182.0),
                                            child: Container(
                                              width: 8,
                                              height: 8,
                                              decoration: BoxDecoration(
                                                shape: BoxShape.circle,
                                                color: const Color(0xFFFFF4C2),
                                                boxShadow: [
                                                  BoxShadow(
                                                    color: gold.withValues(alpha: 0.9),
                                                    blurRadius: 7,
                                                    spreadRadius: 1,
                                                  ),
                                                ],
                                              ),
                                            ),
                                          ),
                                      ],
                                    ),
                                  );
                                },
                              ),

                              const SizedBox(height: 24),

                              const Text(
                                'Developed by Haryono @ 2026',
                                textAlign: TextAlign.center,
                                style: TextStyle(
                                  color: gold,
                                  fontSize: 12,
                                  fontWeight: FontWeight.w600,
                                  letterSpacing: 0.8,
                                ),
                              ),

                              const SizedBox(height: 8),
                            ],
                          ),
                        ),
                      ),
                    ),
                  ),
                );
              },
            ),
          ),
        ],
      ),
    );
  }
}


// ============================================================
// EFEK TONGKAT PERI MENGELILINGI LOGO BULAT
// Kepala bintang terang + ekor runcing + kilau yang berkelap-kelip.
// Semua dihitung dari waktu (tanpa menyimpan state).
// ============================================================

class _WandRingPainter extends CustomPainter {
  _WandRingPainter({required this.progress, required this.radius});

  /// 0.0 - 1.0 dari AnimationController (6 detik).
  final double progress;

  /// Jari-jari tepi logo (piksel).
  final double radius;

  // Pengaturan efek (silakan ubah sesuai selera).
  static const double _total = 6.0; // durasi splash (detik)
  static const double _emitFrom = 0.4; // cahaya mulai muncul
  static const double _emitUntil = 5.3; // cahaya berhenti
  static const double _turns = 3; // jumlah putaran
  static const int _trailSteps = 48; // jumlah segmen ekor
  static const double _trailStep = 0.011; // detik per segmen (ekor ~0,5 dtk)
  static const int _sparkRate = 90; // kilau per detik
  static const double _sparkMaxLife = 1.3; // umur kilau maksimum (detik)

  static double _rnd(int i, int salt) {
    final v = math.sin(i * 12.9898 + salt * 78.233) * 43758.5453;
    return v - v.floorToDouble();
  }

  static double _headAngle(double te) =>
      -math.pi / 2 +
      (te - _emitFrom) / (_emitUntil - _emitFrom) * _turns * 2 * math.pi;

  static Offset _ringPoint(Offset center, double r, double te) {
    final a = _headAngle(te);
    return Offset(center.dx + math.cos(a) * r, center.dy + math.sin(a) * r);
  }

  // Bintang 4 sisi yang runcing (kilau).
  static void _star(Canvas canvas, Offset p, double r, double rot, Paint paint) {
    final w = r * 0.14;
    canvas.save();
    canvas.translate(p.dx, p.dy);
    canvas.rotate(rot);
    final path = Path()
      ..moveTo(-r, 0)
      ..lineTo(0, -w)
      ..lineTo(r, 0)
      ..lineTo(0, w)
      ..close()
      ..moveTo(0, -r)
      ..lineTo(w, 0)
      ..lineTo(0, r)
      ..lineTo(-w, 0)
      ..close();
    canvas.drawPath(path, paint);
    canvas.restore();
  }

  @override
  void paint(Canvas canvas, Size size) {
    final center = size.center(Offset.zero);
    final k = radius / 87.0;
    final t = progress * _total;

    if (t < _emitFrom) return;

    final startFade = ((t - _emitFrom) / 0.25).clamp(0.0, 1.0);
    final tailFade =
        t <= _emitUntil ? 1.0 : (1 - (t - _emitUntil) / 0.5).clamp(0.0, 1.0);
    final headFade =
        t <= _emitUntil ? 1.0 : (1 - (t - _emitUntil) / 0.3).clamp(0.0, 1.0);

    // 1) Ekor cahaya: tebal di kepala, menipis dan memudar ke ujung.
    final glowTrail = Paint()
      ..blendMode = BlendMode.plus
      ..style = PaintingStyle.stroke
      ..strokeCap = StrokeCap.butt
      ..maskFilter = MaskFilter.blur(BlurStyle.normal, 4 * k);
    final coreTrail = Paint()
      ..blendMode = BlendMode.plus
      ..style = PaintingStyle.stroke
      ..strokeCap = StrokeCap.butt;

    Offset? prev;
    for (int j = 0; j <= _trailSteps; j++) {
      final te = t - j * _trailStep;
      if (te < _emitFrom || te > _emitUntil) {
        prev = null;
        continue;
      }
      final p = _ringPoint(center, radius + 1.5 * k, te);
      if (prev != null) {
        final f = 1 - j / _trailSteps;
        final a = f * f * tailFade * startFade;
        final col = Color.lerp(
          const Color(0xFFFF9A2E),
          const Color(0xFFFFF0B0),
          f,
        )!;

        glowTrail
          ..strokeWidth = (3 + 9 * f) * k
          ..color = col.withValues(alpha: 0.35 * a);
        canvas.drawLine(prev, p, glowTrail);

        coreTrail
          ..strokeWidth = (0.8 + 2.6 * f) * k
          ..color = col.withValues(alpha: 0.9 * a);
        canvas.drawLine(prev, p, coreTrail);
      }
      prev = p;
    }

    // 2) Kilau kecil yang berjatuhan dan berkelap-kelip.
    final sparkle = Paint()..blendMode = BlendMode.plus;
    final firstIndex = math.max(0, ((t - _sparkMaxLife) * _sparkRate).floor());
    final lastIndex = (math.min(t, _emitUntil) * _sparkRate).floor();

    for (int i = firstIndex; i <= lastIndex; i++) {
      final te = i / _sparkRate;
      if (te < _emitFrom || te > _emitUntil) continue;

      final life = 0.6 + _rnd(i, 1) * 0.7;
      final age = t - te;
      if (age < 0 || age >= life) continue;
      final a = age / life;

      final ang = _headAngle(te);
      final dir = ang + (_rnd(i, 2) - 0.5) * math.pi * 1.5;
      final sp = (8 + _rnd(i, 3) * 30) * k;
      final ph = _rnd(i, 4);

      final origin = _ringPoint(center, radius + 2 * k, te);
      final pos = Offset(
        origin.dx + math.cos(dir) * sp * age,
        origin.dy + math.sin(dir) * sp * age + 22 * k * age * age,
      );

      final tw = 0.5 + 0.5 * math.sin(age * 22 + ph * 6.28);
      final alpha = (1 - a) * (0.35 + 0.65 * tw) * startFade;
      final r = (2 + _rnd(i, 5) * 3.5) * k * (0.5 + 0.5 * tw) * (1 - a * 0.5);

      final pick = _rnd(i, 6);
      final col = pick < 0.45
          ? const Color(0xFFFFFAE6)
          : pick < 0.85
              ? const Color(0xFFFFD678)
              : const Color(0xFFFFC8AA);

      sparkle.color = col.withValues(alpha: alpha * 0.45);
      canvas.drawCircle(pos, r * 0.9, sparkle);

      sparkle.color = col.withValues(alpha: alpha);
      _star(canvas, pos, r * 2.4, ph * 3.14, sparkle);
    }

    // 3) Kepala: bintang terang yang berkedip.
    final hf = startFade * headFade;
    if (hf > 0) {
      final head = _ringPoint(center, radius + 1.5 * k, math.min(t, _emitUntil));
      final flicker = 0.85 + 0.15 * math.sin(t * 40);

      final halo = Paint()
        ..blendMode = BlendMode.plus
        ..maskFilter = MaskFilter.blur(BlurStyle.normal, 9 * k)
        ..color = const Color(0xFFFFC864).withValues(alpha: 0.6 * hf);
      canvas.drawCircle(head, 14 * k, halo);

      final core = Paint()
        ..blendMode = BlendMode.plus
        ..maskFilter = MaskFilter.blur(BlurStyle.normal, 2 * k)
        ..color = const Color(0xFFFFFFFF).withValues(alpha: 0.95 * hf);
      canvas.drawCircle(head, 4 * k, core);

      final flare = Paint()
        ..blendMode = BlendMode.plus
        ..color = const Color(0xFFFFF6D8).withValues(alpha: 0.9 * hf * flicker);
      _star(canvas, head, 22 * k * flicker, t * 1.6, flare);

      flare.color = const Color(0xFFFFE9A8).withValues(alpha: 0.6 * hf);
      _star(canvas, head, 12 * k, t * 1.6 + math.pi / 4, flare);
    }
  }

  @override
  bool shouldRepaint(covariant _WandRingPainter oldDelegate) {
    return oldDelegate.progress != progress || oldDelegate.radius != radius;
  }
}

class AuthGate extends StatefulWidget {
  const AuthGate({super.key});

  @override
  State<AuthGate> createState() => _AuthGateState();
}

class _AuthGateState extends State<AuthGate> {
  static const String serverUrl = 'https://survey-gps-server.hostless.app';
  String? token;
  String memberId = '';
  String memberName = '';
  String role = '';
  String region = '';

  void onLogin(Map<String, dynamic> result) {
    setState(() {
      token = result['access_token'] as String;
      memberId = result['member_id'].toString();
      memberName = result['member_name'].toString();
      role = result['role'].toString();
      region = result['region'].toString();
    });
  }

  Future<void> logout() async {
    final currentToken = token;
    if (currentToken != null) {
      try {
        await http.post(
          Uri.parse('$serverUrl/auth/logout'),
          headers: {'Authorization': 'Bearer $currentToken'},
        ).timeout(const Duration(seconds: 10));
      } catch (_) {
        // Token lokal tetap dihapus meskipun jaringan sedang tidak tersedia.
      }
    }
    if (!mounted) return;
    setState(() {
      token = null;
      memberId = '';
      memberName = '';
      role = '';
      region = '';
    });
  }

  @override
  Widget build(BuildContext context) {
    if (token == null) return LoginPage(onLogin: onLogin);
    if (role.toUpperCase() == 'DPS') {
      return VerificationHomePage(
        authToken: token!,
        memberId: memberId,
        memberName: memberName,
        region: region,
        onLogout: logout,
      );
    }
    return SurveyHomePage(
      authToken: token!,
      memberId: memberId,
      memberName: memberName,
      role: role,
      region: region,
      onLogout: logout,
    );
  }
}

class LoginPage extends StatefulWidget {
  const LoginPage({super.key, required this.onLogin});
  final ValueChanged<Map<String, dynamic>> onLogin;

  @override
  State<LoginPage> createState() => _LoginPageState();
}

class _LoginPageState extends State<LoginPage> {
  static const String serverUrl = 'https://survey-gps-server.hostless.app';
  final memberIdController = TextEditingController();
  final passwordController = TextEditingController();
  bool busy = false;
  bool obscurePassword = true;
  String message = '';

  @override
  void dispose() {
    memberIdController.dispose();
    passwordController.dispose();
    super.dispose();
  }

  Future<void> login() async {
    final id = memberIdController.text.trim().toUpperCase();
    final password = passwordController.text;
    if (id.isEmpty || password.isEmpty) {
      setState(() => message = 'Masukkan ID petugas dan kata sandi.');
      return;
    }
    setState(() {
      busy = true;
      message = 'Memeriksa akun...';
    });
    try {
      final response = await http.post(
        Uri.parse('$serverUrl/auth/login'),
        headers: {'Content-Type': 'application/json'},
        body: jsonEncode({'member_id': id, 'password': password}),
      ).timeout(const Duration(seconds: 25));
      final decoded = jsonDecode(response.body);
      if (response.statusCode < 200 || response.statusCode >= 300) {
        final detail = decoded is Map ? decoded['detail'] : null;
        throw Exception(detail ?? 'Login gagal (${response.statusCode}).');
      }
      if (decoded is! Map<String, dynamic> || decoded['access_token'] == null) {
        throw Exception('Respons login dari server tidak dikenali.');
      }
      widget.onLogin(decoded);
    } catch (e) {
      if (mounted) setState(() => message = 'Login gagal: $e');
    } finally {
      if (mounted) setState(() => busy = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      body: Stack(
        fit: StackFit.expand,
        children: [
          Image.asset(
            'assets/background_menu_login.png',
            fit: BoxFit.cover,
          ),

          SafeArea(
            child: Center(
              child: SingleChildScrollView(
                padding: const EdgeInsets.all(24),
                child: ConstrainedBox(
                  constraints: const BoxConstraints(maxWidth: 460),
                  child: Container(
                    padding: const EdgeInsets.all(24),
                    decoration: Emboss.raised(
                      radius: 24,
                      colors: const [Color(0xE6FFFFFF), Color(0xD9FBF2E0)],
                      borderColor: gold,
                    ),
                    child: Column(
                      mainAxisSize: MainAxisSize.min,
                      children: [
                        Container(
                          padding: const EdgeInsets.all(4),
                          decoration: BoxDecoration(
                            shape: BoxShape.circle,
                            gradient: const LinearGradient(
                              begin: Alignment.topLeft,
                              end: Alignment.bottomRight,
                              colors: [Color(0xFF4A111A), deepMaroon],
                            ),
                            border: Border.all(color: gold, width: 1.6),
                            boxShadow: const [
                              BoxShadow(
                                color: Color(0x4D31090E),
                                offset: Offset(4, 5),
                                blurRadius: 12,
                              ),
                              BoxShadow(
                                color: Color(0xB3FFFFFF),
                                offset: Offset(-3, -3),
                                blurRadius: 8,
                              ),
                            ],
                          ),
                          child: ClipOval(
                            child: Image.asset(
                              'assets/logo.png',
                              width: 88,
                              height: 88,
                              errorBuilder: (_, _, _) => const Icon(
                                Icons.location_on,
                                size: 76,
                                color: gold,
                              ),
                            ),
                          ),
                        ),
                        const SizedBox(height: 18),

                        const Text(
                          'SURVEI BANTUAN UMKM',
                          textAlign: TextAlign.center,
                          style: TextStyle(
                            color: maroon,
                            fontWeight: FontWeight.bold,
                            fontSize: 22,
                          ),
                        ),

                        const SizedBox(height: 8),

                        const Text(
                          'Masuk menggunakan akun petugas yang diberikan admin.',
                          textAlign: TextAlign.center,
                          style: TextStyle(color: Colors.black54),
                        ),

                        const SizedBox(height: 26),

                        EmbossInput(
                          child: TextField(
                            controller: memberIdController,
                            enabled: !busy,
                            textCapitalization: TextCapitalization.characters,
                            decoration: const InputDecoration(
                              labelText: 'ID petugas',
                              prefixIcon: Icon(Icons.badge_outlined),
                              hintText: 'Contoh: DPSS001',
                            ),
                          ),
                        ),

                        const SizedBox(height: 14),

                        EmbossInput(
                          child: TextField(
                            controller: passwordController,
                            enabled: !busy,
                            obscureText: obscurePassword,
                            onSubmitted: (_) {
                              if (!busy) login();
                            },
                            decoration: InputDecoration(
                              labelText: 'Kata sandi',
                              prefixIcon: const Icon(Icons.lock_outline),
                              suffixIcon: IconButton(
                                onPressed: () => setState(
                                  () => obscurePassword = !obscurePassword,
                                ),
                                icon: Icon(
                                  obscurePassword
                                      ? Icons.visibility_outlined
                                      : Icons.visibility_off_outlined,
                                ),
                              ),
                            ),
                          ),
                        ),

                        const SizedBox(height: 20),

                        SizedBox(
                          width: double.infinity,
                          child: EmbossButton(
                            enabled: !busy,
                            primary: true,
                            child: FilledButton.icon(
                              onPressed: busy ? null : login,
                              icon: busy
                                  ? const SizedBox(
                                      width: 18,
                                      height: 18,
                                      child: CircularProgressIndicator(
                                        strokeWidth: 2,
                                        color: Colors.white,
                                      ),
                                    )
                                  : const Icon(Icons.login),
                              label: Text(busy ? 'MEMERIKSA...' : 'MASUK'),
                              style: Emboss.primaryStyle(vertical: 16),
                            ),
                          ),
                        ),

                        if (message.isNotEmpty) ...[
                          const SizedBox(height: 14),
                          Text(
                            message,
                            textAlign: TextAlign.center,
                            style: TextStyle(
                              color: message.startsWith('Login gagal')
                                  ? Colors.red.shade700
                                  : maroon,
                            ),
                          ),
                        ],

                        const SizedBox(height: 22),

                        const Text(
                          'Akun dibuat oleh admin. Jika belum memiliki akun, hubungi admin.',
                          textAlign: TextAlign.center,
                          style: TextStyle(
                            fontSize: 12,
                            color: Colors.black54,
                          ),
                        ),

                        const SizedBox(height: 12),

                        const Text(
                          'Developed by HARYONO • © 2026',
                          style: TextStyle(
                            fontSize: 11,
                            color: maroon,
                            fontWeight: FontWeight.w600,
                          ),
                        ),
                      ],
                    ),
                  ),
                ),
              ),
            ),
          ),
        ],
      ),
    );
  }
}


class SurveyHomePage extends StatefulWidget {
  const SurveyHomePage({
    super.key,
    required this.authToken,
    required this.memberId,
    required this.memberName,
    required this.role,
    required this.region,
    required this.onLogout,
  });

  final String authToken;
  final String memberId;
  final String memberName;
  final String role;
  final String region;
  final VoidCallback onLogout;

  @override
  State<SurveyHomePage> createState() => _SurveyHomePageState();
}

class _SurveyHomePageState extends State<SurveyHomePage> {
  static const String serverUrl = 'https://survey-gps-server.hostless.app';

  final ImagePicker picker = ImagePicker();
  final Map<String, TextEditingController> c = {
    'member_id': TextEditingController(),
    'applicant_name': TextEditingController(),
    'applicant_nik': TextEditingController(),
    'applicant_phone': TextEditingController(),
    'applicant_address': TextEditingController(),
    'village': TextEditingController(),
    'business_name': TextEditingController(),
    'business_type': TextEditingController(),
    'business_since': TextEditingController(),
    'business_location': TextEditingController(),
    'ownership_status': TextEditingController(),
    'nib': TextEditingController(),
    'capital_amount': TextEditingController(),
    'turnover_amount': TextEditingController(),
    'net_profit_amount': TextEditingController(),
    'worker_count': TextEditingController(),
    'business_issues': TextEditingController(),
    'assistance_amount': TextEditingController(),
    'assistance_reason': TextEditingController(),
    'notes': TextEditingController(),
  };

  Position? position;
  XFile? ktpPhoto;
  XFile? kkPhoto;
  XFile? recipientPhoto;
  XFile? video;
  List<XFile> businessPhotos = [];
  final Set<String> assistanceTypes = {};
  String businessActive = 'Aktif';
  bool gettingGps = false;
  bool sending = false;
  bool statusIsError = false;
  String status = 'Isi data pemohon dan usaha, lalu ambil lokasi GPS.';

  @override
  void initState() {
    super.initState();
    c['member_id']!.text = widget.memberId;
  }

  @override
  void dispose() {
    for (final controller in c.values) {
      controller.dispose();
    }
    super.dispose();
  }

  void setStatus(String message, {bool error = false}) {
    if (!mounted) return;
    setState(() {
      status = message;
      statusIsError = error;
    });
  }

  String value(String key) => c[key]!.text.trim();

  double? numberValue(String key) {
    final text = value(key).replaceAll('.', '').replaceAll(',', '.');
    if (text.isEmpty) return null;
    return double.tryParse(text);
  }

  int? intValue(String key) => int.tryParse(value(key));

  Future<void> getGps() async {
    setState(() {
      gettingGps = true;
      status = 'Sedang mencari lokasi GPS...';
      statusIsError = false;
    });
    try {
      if (!await Geolocator.isLocationServiceEnabled()) {
        throw Exception('Aktifkan GPS/lokasi pada perangkat.');
      }
      var permission = await Geolocator.checkPermission();
      if (permission == LocationPermission.denied) {
        permission = await Geolocator.requestPermission();
      }
      if (permission == LocationPermission.denied ||
          permission == LocationPermission.deniedForever) {
        throw Exception('Izin lokasi belum diberikan.');
      }
      final result = await Geolocator.getCurrentPosition(
        locationSettings: const LocationSettings(
          accuracy: LocationAccuracy.high,
          timeLimit: Duration(seconds: 35),
        ),
      );
      if (!mounted) return;
      setState(() => position = result);
      setStatus('Lokasi GPS berhasil diambil.');
    } catch (e) {
      setStatus('Gagal mengambil GPS: $e', error: true);
    } finally {
      if (mounted) setState(() => gettingGps = false);
    }
  }

  Future<ImageSource?> chooseImageSource() async {
    return showModalBottomSheet<ImageSource>(
      context: context,
      builder: (context) => SafeArea(
        child: Padding(
          padding: const EdgeInsets.fromLTRB(16, 20, 16, 14),
          child: Column(mainAxisSize: MainAxisSize.min, children: [
            EmbossCard(
              radius: 14,
              soft: true,
              margin: const EdgeInsets.only(bottom: 12),
              child: ListTile(
                leading: const Icon(Icons.camera_alt_outlined, color: maroon),
                title: const Text('Ambil dengan kamera'),
                onTap: () => Navigator.pop(context, ImageSource.camera),
              ),
            ),
            EmbossCard(
              radius: 14,
              soft: true,
              margin: EdgeInsets.zero,
              child: ListTile(
                leading: const Icon(Icons.photo_library_outlined, color: maroon),
                title: const Text('Pilih dari galeri'),
                onTap: () => Navigator.pop(context, ImageSource.gallery),
              ),
            ),
          ]),
        ),
      ),
    );
  }

  Future<void> pickIdentityPhoto(String category) async {
    try {
      final source = await chooseImageSource();
      if (source == null) return;
      final photo = await picker.pickImage(
        source: source,
        imageQuality: 85,
        maxWidth: 1920,
      );
      if (photo == null || !mounted) return;
      setState(() {
        if (category == 'applicant_ktp') ktpPhoto = photo;
        if (category == 'family_card') kkPhoto = photo;
        if (category == 'recipient_photo') recipientPhoto = photo;
      });
    } catch (e) {
      setStatus('Gagal memilih foto: $e', error: true);
    }
  }

  Future<void> pickBusinessPhotos() async {
    try {
      final photos = await picker.pickMultiImage(
        imageQuality: 85,
        maxWidth: 1920,
      );
      if (photos.isEmpty || !mounted) return;
      final combined = [...businessPhotos, ...photos];
      final unique = <String, XFile>{};
      for (final photo in combined) {
        unique[photo.path] = photo;
      }
      final all = unique.values.toList();
      if (all.length > 5) {
        setStatus('Maksimal 5 foto dokumentasi usaha. Foto tambahan tidak dipilih.');
      }
      setState(() => businessPhotos = all.take(5).toList());
    } catch (e) {
      setStatus('Gagal memilih foto usaha: $e', error: true);
    }
  }

  Future<void> pickVideo() async {
    try {
      final result = await picker.pickVideo(
        source: ImageSource.gallery,
        maxDuration: const Duration(minutes: 5),
      );
      if (result != null && mounted) setState(() => video = result);
    } catch (e) {
      setStatus('Gagal memilih video: $e', error: true);
    }
  }

  Future<int> createSurvey() async {
    final payload = <String, dynamic>{
      'member_id': value('member_id'),
      'latitude': position!.latitude,
      'longitude': position!.longitude,
      'accuracy': position!.accuracy,
      'notes': value('notes'),
      'surveyed_at': DateTime.now().toUtc().toIso8601String(),
      'applicant_name': value('applicant_name'),
      'applicant_nik': value('applicant_nik'),
      'applicant_phone': value('applicant_phone'),
      'applicant_address': value('applicant_address'),
      'village': value('village'),
      'business_name': value('business_name'),
      'business_type': value('business_type'),
      'business_since': value('business_since'),
      'business_location': value('business_location'),
      'ownership_status': value('ownership_status'),
      'nib': value('nib'),
      'capital_amount': numberValue('capital_amount'),
      'turnover_amount': numberValue('turnover_amount'),
      'net_profit_amount': numberValue('net_profit_amount'),
      'worker_count': intValue('worker_count'),
      'business_active': businessActive,
      'business_issues': value('business_issues'),
      'assistance_types': assistanceTypes.join(', '),
      'assistance_amount': numberValue('assistance_amount'),
      'assistance_reason': value('assistance_reason'),
      'business_verified': 'Belum diverifikasi',
      'verification_status': 'Perlu ditinjau',
      'verification_notes': '',
    };

    final response = await http
        .post(
          Uri.parse('$serverUrl/surveys'),
          headers: {
            'Content-Type': 'application/json',
            'Authorization': 'Bearer ${widget.authToken}',
          },
          body: jsonEncode(payload),
        )
        .timeout(const Duration(seconds: 40));
    if (response.statusCode < 200 || response.statusCode >= 300) {
      throw Exception('Server menolak data (${response.statusCode}): ${response.body}');
    }
    final result = jsonDecode(response.body) as Map<String, dynamic>;
    final id = result['survey_id'];
    if (id == null) throw Exception('Server tidak mengembalikan nomor survei.');
    return int.parse(id.toString());
  }

  Future<void> uploadMedia(int surveyId, XFile file, String category) async {
    final mime = lookupMimeType(file.name) ?? lookupMimeType(file.path) ??
        (category == 'business_video' ? 'video/mp4' : 'image/jpeg');
    final request = http.MultipartRequest(
      'POST',
      Uri.parse('$serverUrl/surveys/$surveyId/media'),
    )
      ..headers['Authorization'] = 'Bearer ${widget.authToken}'
      ..fields['member_id'] = value('member_id')
      ..fields['media_category'] = category
      ..files.add(await http.MultipartFile.fromPath(
        'file',
        file.path,
        filename: file.name,
        contentType: MediaType.parse(mime),
      ));
    final response = await request.send().timeout(const Duration(minutes: 4));
    final body = await response.stream.bytesToString();
    if (response.statusCode < 200 || response.statusCode >= 300) {
      throw Exception('Gagal mengunggah ${file.name} (${response.statusCode}): $body');
    }
  }

  Future<void> sendSurvey() async {
    if (value('member_id').isEmpty) {
      setStatus('ID anggota wajib diisi.', error: true);
      return;
    }
    if (value('applicant_name').isEmpty || value('applicant_nik').isEmpty) {
      setStatus('Nama pemohon dan NIK wajib diisi.', error: true);
      return;
    }
    if (value('village').isEmpty || value('business_name').isEmpty ||
        value('business_type').isEmpty) {
      setStatus('Desa/kelurahan, nama usaha, dan jenis usaha wajib diisi.', error: true);
      return;
    }
    if (assistanceTypes.isEmpty) {
      setStatus('Pilih minimal satu jenis bantuan yang dibutuhkan.', error: true);
      return;
    }
    if (position == null) {
      setStatus('Ambil lokasi GPS sebelum mengirim data.', error: true);
      return;
    }

    setState(() {
      sending = true;
      status = 'Menyimpan data survei...';
      statusIsError = false;
    });
    try {
      final id = await createSurvey();
      final mediaItems = <({XFile file, String category})>[];
      if (ktpPhoto != null) mediaItems.add((file: ktpPhoto!, category: 'applicant_ktp'));
      if (kkPhoto != null) mediaItems.add((file: kkPhoto!, category: 'family_card'));
      if (recipientPhoto != null) mediaItems.add((file: recipientPhoto!, category: 'recipient_photo'));
      for (final photo in businessPhotos) {
        mediaItems.add((file: photo, category: 'business_photo'));
      }
      if (video != null) mediaItems.add((file: video!, category: 'business_video'));

      for (var i = 0; i < mediaItems.length; i++) {
        if (!mounted) return;
        setState(() => status = 'Mengunggah berkas ${i + 1} dari ${mediaItems.length}...');
        await uploadMedia(id, mediaItems[i].file, mediaItems[i].category);
      }
      if (!mounted) return;
      setState(() {
        status = 'Survei berhasil dikirim. Nomor survei: $id. Data dan ${mediaItems.length} berkas telah diproses.';
        statusIsError = false;
        for (final controller in c.values) {
          controller.clear();
        }
        c['member_id']!.text = widget.memberId;
        position = null;
        ktpPhoto = null;
        kkPhoto = null;
        recipientPhoto = null;
        businessPhotos = [];
        video = null;
        assistanceTypes.clear();
        businessActive = 'Aktif';
      });
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          const SnackBar(content: Text('Data survei berhasil dikirim.')),
        );
      }
    } catch (e) {
      setStatus('Pengiriman belum selesai. $e\nJika nomor survei sudah tercipta, jangan kirim ulang sebelum memeriksa dashboard agar tidak membuat data ganda.', error: true);
    } finally {
      if (mounted) setState(() => sending = false);
    }
  }

  Widget field(String key, String label,
      {String? hint, int lines = 1, TextInputType? keyboard, bool required = false}) {
    return Padding(
      padding: const EdgeInsets.only(bottom: 12),
      child: EmbossInput(
        child: TextField(
          controller: c[key],
          enabled: !sending && key != 'member_id',
          maxLines: lines,
          keyboardType: keyboard ?? (lines > 1 ? TextInputType.multiline : TextInputType.text),
          textCapitalization: TextCapitalization.sentences,
          decoration: InputDecoration(
            labelText: required ? '$label *' : label,
            hintText: hint,
            alignLabelWithHint: lines > 1,
          ),
        ),
      ),
    );
  }

  Widget section(String title, IconData icon, List<Widget> children) => EmbossCard(
        margin: const EdgeInsets.only(bottom: 14),
        padding: const EdgeInsets.all(16),
        child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
          Row(children: [EmbossIconBadge(icon: icon), const SizedBox(width: 10), Expanded(child: Text(title, style: const TextStyle(color: maroon, fontWeight: FontWeight.bold, letterSpacing: 0.3)))]),
          const SizedBox(height: 10),
          const EmbossDivider(),
          const SizedBox(height: 14),
          ...children,
        ]),
      );

  Widget choice(String label, String current, List<String> options, ValueChanged<String?> onChanged) =>
      Padding(
        padding: const EdgeInsets.only(bottom: 12),
        child: EmbossInput(
          child: DropdownButtonFormField<String>(
            initialValue: current,
            dropdownColor: ivory,
            borderRadius: BorderRadius.circular(14),
            decoration: InputDecoration(labelText: label),
            items: options.map((e) => DropdownMenuItem(value: e, child: Text(e))).toList(),
            onChanged: sending ? null : onChanged,
          ),
        ),
      );

  Widget imagePickerRow(String title, XFile? file, VoidCallback onPick, VoidCallback onRemove) =>
      EmbossCard(
        margin: const EdgeInsets.only(bottom: 12),
        padding: const EdgeInsets.all(10),
        radius: 14,
        pressed: file == null,
        soft: true,
        child: Row(children: [
          Icon(file == null ? Icons.add_a_photo_outlined : Icons.check_circle, color: file == null ? maroon : Colors.green),
          const SizedBox(width: 10),
          Expanded(child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [Text(title, style: const TextStyle(fontWeight: FontWeight.w600)), Text(file?.name ?? 'Belum dipilih', maxLines: 2, overflow: TextOverflow.ellipsis, style: const TextStyle(fontSize: 12, color: Colors.black54))])),
          if (file != null) IconButton(tooltip: 'Hapus pilihan', onPressed: sending ? null : onRemove, icon: const Icon(Icons.delete_outline, color: Colors.red)),
          IconButton(tooltip: 'Pilih foto', onPressed: sending ? null : onPick, icon: const Icon(Icons.folder_open, color: maroon)),
        ]),
      );

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(
        title: const Text('Survei Bantuan UMKM'),
        actions: [
          if (MediaQuery.sizeOf(context).width > 480)
            Center(child: Text('${widget.memberId} • ${widget.role}', style: const TextStyle(fontSize: 12))),
          IconButton(
            tooltip: 'Keluar',
            onPressed: sending ? null : widget.onLogout,
            icon: const Icon(Icons.logout),
          ),
          Padding(
            padding: const EdgeInsets.only(right: 8),
            child: Image.asset('assets/logo.png', width: 36, height: 36,
                errorBuilder: (_, _, _) => const Icon(Icons.location_on, color: gold)),
          ),
        ],
      ),
      body: Stack(
        children: [
          Positioned.fill(
            child: Image.asset(
              'assets/background_menu_dpss.png',
              fit: BoxFit.cover,
            ),
          ),
         SafeArea(
      child: ListView(
          padding: const EdgeInsets.all(14),
          children: [
            Container(
              padding: const EdgeInsets.all(18),
              margin: const EdgeInsets.only(bottom: 14),
              decoration: Emboss.primary(radius: 20, strong: true),
              child: const Row(children: [
                Icon(Icons.storefront_rounded, color: gold, size: 42),
                SizedBox(width: 13),
                Expanded(child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                  Text('PENDATAAN CALON PENERIMA BANTUAN UMKM', style: TextStyle(color: Colors.white, fontWeight: FontWeight.bold, fontSize: 17)),
                  SizedBox(height: 6),
                  Text('Survei lapangan Kecamatan Tambang, Kabupaten Kampar', style: TextStyle(color: Color(0xFFF5E8D2), fontSize: 12)),
                ])),
              ]),
            ),
            section('IDENTITAS PETUGAS', Icons.badge_outlined, [
              field('member_id', 'ID anggota/petugas', hint: 'Diisi otomatis setelah login', required: true),
              Text('${widget.memberName} • ${widget.role} • ${widget.region}', style: const TextStyle(fontSize: 12, color: Colors.black54)),
            ]),
            section('IDENTITAS PEMOHON BANTUAN', Icons.person_outline, [
              field('applicant_name', 'Nama lengkap sesuai KTP', required: true),
              field('applicant_nik', 'NIK', keyboard: TextInputType.number, hint: 'Nomor Induk Kependudukan 16 digit', required: true),
              field('applicant_phone', 'Nomor HP aktif', keyboard: TextInputType.phone),
              field('applicant_address', 'Alamat lengkap', lines: 2),
              field('village', 'Desa/kelurahan dan dusun', required: true),
            ]),
            section('DOKUMEN IDENTITAS PEMOHON', Icons.folder_shared_outlined, [
              const Text('Foto KTP, KK, dan penerima bantuan disimpan sebagai kategori terpisah dari foto usaha.', style: TextStyle(fontSize: 12, color: Colors.black54)),
              const SizedBox(height: 10),
              imagePickerRow('1. Foto KTP pemohon', ktpPhoto, () => pickIdentityPhoto('applicant_ktp'), () => setState(() => ktpPhoto = null)),
              imagePickerRow('2. Foto Kartu Keluarga (KK)', kkPhoto, () => pickIdentityPhoto('family_card'), () => setState(() => kkPhoto = null)),
              imagePickerRow('3. Foto orang penerima bantuan', recipientPhoto, () => pickIdentityPhoto('recipient_photo'), () => setState(() => recipientPhoto = null)),
            ]),
            section('PROFIL USAHA UMKM', Icons.store_mall_directory_outlined, [
              field('business_name', 'Nama usaha', required: true),
              field('business_type', 'Jenis usaha/produk', hint: 'Contoh: kuliner, kerajinan, perdagangan', required: true),
              field('business_since', 'Tahun mulai usaha', keyboard: TextInputType.number),
              field('business_location', 'Alamat/lokasi usaha', lines: 2),
              field('ownership_status', 'Status tempat usaha', hint: 'Milik sendiri, sewa, menumpang, dll.'),
              field('nib', 'Nomor Induk Berusaha (NIB)', keyboard: TextInputType.text),
              choice('Apakah usaha masih aktif?', businessActive, ['Aktif', 'Tidak aktif', 'Musiman'], (v) => setState(() => businessActive = v ?? 'Aktif')),
            ]),
            section('KONDISI USAHA DAN EKONOMI', Icons.query_stats_outlined, [
              field('capital_amount', 'Perkiraan modal usaha (Rp)', keyboard: TextInputType.number, hint: 'Contoh: 5000000'),
              field('turnover_amount', 'Perkiraan omzet per bulan (Rp)', keyboard: TextInputType.number),
              field('net_profit_amount', 'Perkiraan laba bersih per bulan (Rp)', keyboard: TextInputType.number),
              field('worker_count', 'Jumlah pekerja', keyboard: TextInputType.number),
              field('business_issues', 'Kendala utama usaha', lines: 3),
            ]),
            section('KEBUTUHAN BANTUAN', Icons.volunteer_activism_outlined, [
              const Text('Pilih satu atau beberapa jenis bantuan yang dibutuhkan:', style: TextStyle(color: Colors.black54, fontSize: 12)),
              const SizedBox(height: 5),
              ...['Bantuan uang tunai', 'Peralatan usaha', 'Bahan baku', 'Pengembangan produk', 'Legalitas/perizinan', 'Pemasaran/promosi', 'Pelatihan usaha', 'Lainnya'].map((item) => EmbossCard(
                    pressed: assistanceTypes.contains(item),
                    radius: 12,
                    soft: true,
                    margin: const EdgeInsets.only(bottom: 8),
                    child: CheckboxListTile(
                    value: assistanceTypes.contains(item),
                    contentPadding: const EdgeInsets.symmetric(horizontal: 6),
                    dense: true,
                    activeColor: maroon,
                    title: Text(item),
                    controlAffinity: ListTileControlAffinity.leading,
                    onChanged: sending ? null : (checked) => setState(() {
                      if (checked == true) { assistanceTypes.add(item); } else { assistanceTypes.remove(item); }
                    }),
                  ))),
              field('assistance_amount', 'Perkiraan nilai bantuan yang dibutuhkan (Rp)', keyboard: TextInputType.number),
              field('assistance_reason', 'Alasan dan tujuan pengajuan bantuan', lines: 3),
            ]),
            section('LOKASI GPS DAN VERIFIKASI', Icons.gps_fixed, [
              SizedBox(width: double.infinity, child: EmbossButton(enabled: !(gettingGps || sending), child: OutlinedButton.icon(
                onPressed: gettingGps || sending ? null : getGps,
                icon: gettingGps ? const SizedBox(width: 18, height: 18, child: CircularProgressIndicator(strokeWidth: 2)) : const Icon(Icons.my_location),
                label: Text(gettingGps ? 'Mencari GPS...' : 'AMBIL LOKASI GPS'),
                style: Emboss.secondaryStyle(vertical: 14),
              ))),
              const SizedBox(height: 10),
              if (position == null)
                const Text('Lokasi belum diambil. GPS wajib diambil saat survei di lapangan.', style: TextStyle(color: Colors.black54))
              else
                EmbossCard(margin: EdgeInsets.zero, padding: const EdgeInsets.all(12), pressed: true, radius: 12, colors: const [Color(0xFFF3E6C8), Color(0xFFFFF9EA)], child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                  Text('Latitude: ${position!.latitude}'),
                  Text('Longitude: ${position!.longitude}'),
                  Text('Akurasi: ${position!.accuracy.toStringAsFixed(1)} meter'),
                ])),
              const SizedBox(height: 12),
              EmbossCard(
                margin: EdgeInsets.zero,
                padding: const EdgeInsets.all(12),
                pressed: true,
                radius: 12,
                colors: const [Color(0xFFF3E6C8), Color(0xFFFFF9EA)],
                child: const Text('Status verifikasi akan ditetapkan melalui proses pemeriksaan DPS, bukan oleh petugas pengumpul data.', style: TextStyle(fontSize: 12, height: 1.4)),
              ),
              const SizedBox(height: 10),
              field('notes', 'Catatan tambahan survei', lines: 3),
            ]),
            section('FOTO DOKUMENTASI USAHA', Icons.photo_camera_back_outlined, [
              const Text('Maksimal 5 foto usaha/dokumen pendukung. Foto KTP, KK, dan penerima bantuan tidak dihitung dalam batas ini.', style: TextStyle(fontSize: 12, color: Colors.black54)),
              const SizedBox(height: 10),
              EmbossButton(enabled: !(sending || businessPhotos.length >= 5), child: OutlinedButton.icon(onPressed: sending || businessPhotos.length >= 5 ? null : pickBusinessPhotos, icon: const Icon(Icons.add_photo_alternate_outlined), label: Text('Pilih foto usaha (${businessPhotos.length}/5)'), style: Emboss.secondaryStyle(vertical: 13))),
              const SizedBox(height: 8),
              ...businessPhotos.asMap().entries.map((entry) => EmbossCard(
                    pressed: true,
                    radius: 12,
                    soft: true,
                    margin: const EdgeInsets.only(bottom: 8),
                    padding: const EdgeInsets.symmetric(horizontal: 10),
                    child: ListTile(
                    dense: true,
                    contentPadding: EdgeInsets.zero,
                    leading: const Icon(Icons.image_outlined, color: maroon),
                    title: Text('Foto usaha ${entry.key + 1}'),
                    subtitle: Text(entry.value.name, maxLines: 2, overflow: TextOverflow.ellipsis),
                    trailing: IconButton(tooltip: 'Hapus foto', onPressed: sending ? null : () => setState(() => businessPhotos.removeAt(entry.key)), icon: const Icon(Icons.delete_outline, color: Colors.red)),
                  ))),
            ]),
            section('VIDEO DOKUMENTASI', Icons.video_library_outlined, [
              const Text('Opsional, maksimal 5 menit. Pilih video dari galeri.', style: TextStyle(fontSize: 12, color: Colors.black54)),
              const SizedBox(height: 8),
              EmbossButton(enabled: !sending, child: OutlinedButton.icon(onPressed: sending ? null : pickVideo, icon: const Icon(Icons.video_file_outlined), label: const Text('PILIH VIDEO (MAKS. 5 MENIT)'), style: Emboss.secondaryStyle(vertical: 13))),
              if (video != null) EmbossCard(
                pressed: true,
                radius: 12,
                soft: true,
                margin: const EdgeInsets.only(top: 10),
                padding: const EdgeInsets.symmetric(horizontal: 10),
                child: ListTile(
                contentPadding: EdgeInsets.zero,
                leading: const Icon(Icons.video_file, color: maroon),
                title: Text(video!.name, maxLines: 2, overflow: TextOverflow.ellipsis),
                trailing: IconButton(tooltip: 'Hapus video', onPressed: sending ? null : () => setState(() => video = null), icon: const Icon(Icons.delete_outline, color: Colors.red)),
              )),
            ]),
            EmbossButton(enabled: !sending, primary: true, child: FilledButton.icon(
              onPressed: sending ? null : sendSurvey,
              icon: sending ? const SizedBox(width: 18, height: 18, child: CircularProgressIndicator(strokeWidth: 2, color: Colors.white)) : const Icon(Icons.cloud_upload_outlined),
              label: Text(sending ? 'SEDANG MENGIRIM...' : 'SIMPAN DAN KIRIM DATA SURVEI'),
              style: Emboss.primaryStyle(vertical: 17),
            )),
            const SizedBox(height: 14),
            EmbossCard(
              margin: const EdgeInsets.symmetric(horizontal: 2, vertical: 4),
              padding: const EdgeInsets.all(14),
              pressed: true,
              radius: 14,
              colors: statusIsError
                  ? const [Color(0xFFFBE3E1), Color(0xFFFFF4F2)]
                  : const [Color(0xFFF3E6C8), Color(0xFFFFF9EA)],
              borderColor: statusIsError ? Colors.red.shade300 : gold,
              child: Row(crossAxisAlignment: CrossAxisAlignment.start, children: [
                Icon(statusIsError ? Icons.error_outline : Icons.info_outline, color: statusIsError ? Colors.red : maroon),
                const SizedBox(width: 10),
                Expanded(child: Text(status, style: TextStyle(color: statusIsError ? Colors.red.shade800 : maroon, height: 1.4))),
              ]),
            ),
            const SizedBox(height: 14),
            const Text('Data identitas dan dokumen hanya boleh diakses admin yang berwenang. Pastikan koneksi internet aktif saat mengirim.', textAlign: TextAlign.center, style: TextStyle(fontSize: 11, color: Colors.black54, height: 1.4)),
            const SizedBox(height: 8),
            const Text('Developed by HARYONO • © 2026', textAlign: TextAlign.center, style: TextStyle(fontSize: 11, color: maroon, fontWeight: FontWeight.w600)),
          ],
        ),
      ),
    ],
  ),
);
}
}

// =========================================================
// MODUL VERIFIKASI LAPANGAN DPS
// Modul tambahan. Formulir survei awal DPSS di atas tetap dipertahankan.
// =========================================================

class VerificationHomePage extends StatefulWidget {
  const VerificationHomePage({
    super.key,
    required this.authToken,
    required this.memberId,
    required this.memberName,
    required this.region,
    required this.onLogout,
  });

  final String authToken;
  final String memberId;
  final String memberName;
  final String region;
  final VoidCallback onLogout;

  @override
  State<VerificationHomePage> createState() => _VerificationHomePageState();
}

class _VerificationHomePageState extends State<VerificationHomePage> {
  static const String serverUrl = 'https://survey-gps-server.hostless.app';
  bool loading = false;
  bool includeCompleted = false;
  String message = '';
  List<Map<String, dynamic>> surveys = [];

  Map<String, String> get headers => {
        'Authorization': 'Bearer ${widget.authToken}',
        'Content-Type': 'application/json',
      };

  @override
  void initState() {
    super.initState();
    loadSurveys();
  }

  Future<void> loadSurveys() async {
    setState(() {
      loading = true;
      message = 'Mengambil data survei dari server...';
    });
    try {
      final response = await http.get(
        Uri.parse('$serverUrl/dps/surveys${includeCompleted ? '?include_completed=true' : ''}'),
        headers: headers,
      ).timeout(const Duration(seconds: 35));
      final decoded = jsonDecode(response.body);
      if (response.statusCode < 200 || response.statusCode >= 300) {
        final detail = decoded is Map ? decoded['detail'] : null;
        throw Exception(detail ?? 'Gagal mengambil data (${response.statusCode}).');
      }
      if (decoded is! List) throw Exception('Format daftar survei dari server tidak sesuai.');
      if (!mounted) return;
      setState(() {
        surveys = decoded.whereType<Map>().map((e) => Map<String, dynamic>.from(e)).toList();
        message = surveys.isEmpty
            ? (includeCompleted ? 'Belum ada data survei.' : 'Tidak ada survei yang menunggu verifikasi.')
            : '${surveys.length} data survei ditemukan.';
      });
    } catch (e) {
      if (mounted) setState(() => message = 'Gagal memuat data: $e');
    } finally {
      if (mounted) setState(() => loading = false);
    }
  }

  String textOf(Map<String, dynamic> row, String key) {
    final value = row[key];
    if (value == null || value.toString().trim().isEmpty) return '-';
    return value.toString();
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(
        title: const Text('Verifikasi UMKM • DPS'),
        actions: [
          IconButton(tooltip: 'Muat ulang', onPressed: loading ? null : loadSurveys, icon: const Icon(Icons.refresh)),
          IconButton(tooltip: 'Keluar', onPressed: widget.onLogout, icon: const Icon(Icons.logout)),
        ],
      ),
body: Stack(
  children: [
    Positioned.fill(
      child: Image.asset(
        'assets/background_menu_dps.png',
        fit: BoxFit.cover,
      ),
    ),

    RefreshIndicator(
      onRefresh: loadSurveys,
      child: ListView(
        padding: const EdgeInsets.all(16),
        children: [
          EmbossCard(
            margin: const EdgeInsets.only(bottom: 8),
            padding: const EdgeInsets.all(16),
            colors: const [Color(0xFFFFFDF4), Color(0xFFFFF0D2)],
            borderColor: gold,
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                const Text(
                  'PETUGAS VERIFIKASI DPS',
                  style: TextStyle(
                    fontWeight: FontWeight.bold,
                    color: maroon,
                    fontSize: 16,
                  ),
                ),
                const SizedBox(height: 6),
                Text('${widget.memberName} • ${widget.memberId}'),
                Text('Wilayah tugas: ${widget.region.isEmpty ? '-' : widget.region}'),
                const SizedBox(height: 8),
                const Text(
                  'Pilih data survei awal, kunjungi lokasi usaha, lalu lengkapi formulir pemeriksaan yang terpisah dari data DPSS.',
                  style: TextStyle(height: 1.4),
                ),
              ],
            ),
          ),
          EmbossCard(
            pressed: true,
            radius: 14,
            margin: const EdgeInsets.symmetric(horizontal: 2, vertical: 8),
            child: SwitchListTile(
            contentPadding: const EdgeInsets.symmetric(horizontal: 14),
            title: const Text('Tampilkan survei yang sudah diputuskan'),
            value: includeCompleted,
            onChanged: (value) {
              setState(() => includeCompleted = value);
              loadSurveys();
            },
          )),
          if (loading)
            ClipRRect(
              borderRadius: BorderRadius.circular(8),
              child: const LinearProgressIndicator(
                minHeight: 5,
                color: maroon,
                backgroundColor: Color(0xFFEFE2C4),
              ),
            ),
          Padding(
            padding: const EdgeInsets.symmetric(vertical: 8),
            child: Text(
              message,
              style: const TextStyle(color: Colors.black54),
            ),
          ),
          ...surveys.map((survey) {
            final status = textOf(survey, 'verification_status');
            final id = survey['id'];
            return EmbossCard(
              margin: const EdgeInsets.fromLTRB(2, 4, 2, 10),
              child: ListTile(
                contentPadding: const EdgeInsets.all(12),
                leading: Container(
                  decoration: BoxDecoration(
                    shape: BoxShape.circle,
                    border: Border.all(color: gold, width: 1.4),
                    boxShadow: const [
                      BoxShadow(
                        color: Color(0x4D31090E),
                        offset: Offset(2, 3),
                        blurRadius: 6,
                      ),
                    ],
                  ),
                  child: CircleAvatar(
                    backgroundColor: maroon,
                    foregroundColor: Colors.white,
                    child: Text('$id'),
                  ),
                ),
                title: Text(
                  textOf(survey, 'applicant_name'),
                  style: const TextStyle(fontWeight: FontWeight.bold),
                ),
                subtitle: Padding(
                  padding: const EdgeInsets.only(top: 5),
                  child: Text(
                    '${textOf(survey, 'business_name')}\n${textOf(survey, 'village')}\nStatus: $status',
                    maxLines: 4,
                    overflow: TextOverflow.ellipsis,
                  ),
                ),
                trailing: const Icon(Icons.chevron_right, color: maroon),
                onTap: () async {
                  final done = await Navigator.of(context).push<bool>(
                    MaterialPageRoute(
                      builder: (_) => VerificationDetailPage(
                        authToken: widget.authToken,
                        surveyId: int.parse(id.toString()),
                        onSubmitted: () {},
                      ),
                    ),
                  );
                  if (done == true && mounted) loadSurveys();
                },
              ),
            );
          }),
        ],
      ),
    ),
  ],
),
);
}
}

class _NeedRow {
  final name = TextEditingController();
  final specification = TextEditingController();
  final quantity = TextEditingController(text: '1');
  final unit = TextEditingController(text: 'unit');
  final unitPrice = TextEditingController();
  final notes = TextEditingController();

  Map<String, dynamic> toJson() => {
        'item_name': name.text.trim(),
        'specification': specification.text.trim(),
        'quantity': double.tryParse(quantity.text.replaceAll(',', '.')) ?? 0,
        'unit': unit.text.trim().isEmpty ? 'unit' : unit.text.trim(),
        'unit_price': double.tryParse(unitPrice.text.replaceAll('.', '').replaceAll(',', '.')) ?? 0,
        'notes': notes.text.trim(),
      };

  void dispose() {
    name.dispose(); specification.dispose(); quantity.dispose(); unit.dispose(); unitPrice.dispose(); notes.dispose();
  }
}

class OriginalMediaVideoPlayer extends StatefulWidget {
  const OriginalMediaVideoPlayer({super.key, required this.url});

  final String url;

  @override
  State<OriginalMediaVideoPlayer> createState() => _OriginalMediaVideoPlayerState();
}

class _OriginalMediaVideoPlayerState extends State<OriginalMediaVideoPlayer> {
  late final VideoPlayerController _controller;
  String? _error;

  @override
  void initState() {
    super.initState();
    _controller = VideoPlayerController.networkUrl(Uri.parse(widget.url));
    _controller.initialize().then((_) {
      if (mounted) setState(() {});
    }).catchError((Object error) {
      if (mounted) setState(() => _error = error.toString());
    });
  }

  @override
  void dispose() {
    _controller.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    if (_error != null) {
      return Container(
        width: double.infinity,
        padding: const EdgeInsets.all(12),
        decoration: BoxDecoration(
          color: Colors.red.withValues(alpha: 0.06),
          borderRadius: BorderRadius.circular(10),
          border: Border.all(color: Colors.red.withValues(alpha: 0.25)),
        ),
        child: Text('Video gagal dimuat: $_error', style: const TextStyle(color: Colors.red)),
      );
    }

    if (!_controller.value.isInitialized) {
      return Container(
        height: 190,
        width: double.infinity,
        decoration: BoxDecoration(color: Colors.black87, borderRadius: BorderRadius.circular(10)),
        child: const Center(child: CircularProgressIndicator(color: gold)),
      );
    }

    return Container(
      padding: const EdgeInsets.all(2),
      decoration: Emboss.raised(radius: 12, soft: true, borderColor: gold),
      child: ClipRRect(
      borderRadius: BorderRadius.circular(10),
      child: Container(
        color: Colors.black,
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            AspectRatio(
              aspectRatio: _controller.value.aspectRatio > 0 ? _controller.value.aspectRatio : 16 / 9,
              child: VideoPlayer(_controller),
            ),
            Row(
              children: [
                IconButton(
                  tooltip: _controller.value.isPlaying ? 'Jeda video' : 'Putar video',
                  color: Colors.white,
                  onPressed: () {
                    setState(() {
                      if (_controller.value.isPlaying) {
                        _controller.pause();
                      } else {
                        _controller.play();
                      }
                    });
                  },
                  icon: Icon(_controller.value.isPlaying ? Icons.pause : Icons.play_arrow),
                ),
                Expanded(
                  child: VideoProgressIndicator(
                    _controller,
                    allowScrubbing: true,
                    padding: const EdgeInsets.symmetric(vertical: 12, horizontal: 4),
                    colors: const VideoProgressColors(
                      playedColor: gold,
                      bufferedColor: Colors.white38,
                      backgroundColor: Colors.white24,
                    ),
                  ),
                ),
                ValueListenableBuilder<VideoPlayerValue>(
                  valueListenable: _controller,
                  builder: (context, value, child) => Padding(
                    padding: const EdgeInsets.only(left: 6, right: 10),
                    child: Text(
                      value.isInitialized ? _formatVideoDuration(value.position) : '00:00',
                      style: const TextStyle(color: Colors.white, fontSize: 11),
                    ),
                  ),
                ),
              ],
            ),
          ],
        ),
      ),
      ),
    );
  }

  String _formatVideoDuration(Duration duration) {
    final minutes = duration.inMinutes.remainder(60).toString().padLeft(2, '0');
    final seconds = duration.inSeconds.remainder(60).toString().padLeft(2, '0');
    return '$minutes:$seconds';
  }
}

class VerificationDetailPage extends StatefulWidget {
  const VerificationDetailPage({super.key, required this.authToken, required this.surveyId, required this.onSubmitted});
  final String authToken;
  final int surveyId;
  final VoidCallback onSubmitted;

  @override
  State<VerificationDetailPage> createState() => _VerificationDetailPageState();
}

class _VerificationDetailPageState extends State<VerificationDetailPage> {
  static const String serverUrl = 'https://survey-gps-server.hostless.app';
  final ImagePicker picker = ImagePicker();
  final Map<String, TextEditingController> c = {
    'business_condition': TextEditingController(),
    'actual_business_activity': TextEditingController(),
    'existing_assets': TextEditingController(),
    'working_capital_details': TextEditingController(),
    'verified_turnover': TextEditingController(),
    'verified_profit': TextEditingController(),
    'verified_worker_count': TextEditingController(),
    'discrepancy_notes': TextEditingController(),
    'verification_notes': TextEditingController(),
    'rejection_reason': TextEditingController(),
  };
  Map<String, dynamic>? survey;
  List<Map<String, dynamic>> originalMedia = [];
  final List<_NeedRow> needs = [_NeedRow()];
  final List<XFile> verificationPhotos = [];
  final Set<String> uploadedPhotoPaths = {};
  Position? position;
  String? dataMatch;
  String? decision;
  bool loading = true;
  bool sending = false;
  bool gettingGps = false;
  String message = '';

  Map<String, String> get headers => {
        'Authorization': 'Bearer ${widget.authToken}',
        'Content-Type': 'application/json',
      };

  @override
  void initState() {
    super.initState();
    loadDetail();
  }

  @override
  void dispose() {
    for (final controller in c.values) { controller.dispose(); }
    for (final row in needs) { row.dispose(); }
    super.dispose();
  }

  String val(String key) => c[key]!.text.trim();
  double? numVal(String key) {
    final raw = val(key).replaceAll('.', '').replaceAll(',', '.');
    return raw.isEmpty ? null : double.tryParse(raw);
  }
  int? intVal(String key) => int.tryParse(val(key));
  String surveyText(String key) {
    final value = survey?[key];
    return value == null || value.toString().trim().isEmpty ? '-' : value.toString();
  }

  Future<void> loadDetail() async {
    setState(() { loading = true; message = 'Memuat data survei awal...'; });
    try {
      final response = await http.get(Uri.parse('$serverUrl/dps/surveys/${widget.surveyId}'), headers: headers).timeout(const Duration(seconds: 35));
      final decoded = jsonDecode(response.body);
      if (response.statusCode < 200 || response.statusCode >= 300) {
        final detail = decoded is Map ? decoded['detail'] : null;
        throw Exception(detail ?? 'Gagal memuat survei (${response.statusCode}).');
      }
      if (decoded is! Map<String, dynamic> || decoded['survey'] is! Map) throw Exception('Format data survei tidak dikenali.');
      if (!mounted) return;
      setState(() {
        survey = Map<String, dynamic>.from(decoded['survey'] as Map);
        originalMedia = (decoded['survey_media'] as List? ?? []).whereType<Map>().map((e) => Map<String, dynamic>.from(e)).toList();
        message = '';
      });
    } catch (e) {
      if (mounted) setState(() => message = 'Gagal memuat data: $e');
    } finally {
      if (mounted) setState(() => loading = false);
    }
  }

  Future<void> getGps() async {
    setState(() { gettingGps = true; message = 'Mengambil GPS verifikasi...'; });
    try {
      var permission = await Geolocator.checkPermission();
      if (permission == LocationPermission.denied) permission = await Geolocator.requestPermission();
      if (permission == LocationPermission.denied || permission == LocationPermission.deniedForever) throw Exception('Izin lokasi belum diberikan. Aktifkan izin lokasi untuk aplikasi.');
      final serviceEnabled = await Geolocator.isLocationServiceEnabled();
      if (!serviceEnabled) throw Exception('GPS perangkat belum aktif.');
      final found = await Geolocator.getCurrentPosition(locationSettings: const LocationSettings(accuracy: LocationAccuracy.high)).timeout(const Duration(seconds: 35));
      if (!mounted) return;
      setState(() { position = found; message = 'GPS verifikasi berhasil diambil.'; });
    } catch (e) {
      if (mounted) setState(() => message = 'GPS gagal: $e');
    } finally {
      if (mounted) setState(() => gettingGps = false);
    }
  }

  Future<void> pickPhotos() async {
    try {
      final picked = await picker.pickMultiImage(imageQuality: 88);
      if (picked.isEmpty) return;
      final byPath = <String, XFile>{for (final photo in verificationPhotos) photo.path: photo};
      for (final photo in picked) { byPath[photo.path] = photo; }
      if (!mounted) return;
      setState(() {
        verificationPhotos
          ..clear()
          ..addAll(byPath.values.take(5));
        message = byPath.length > 5 ? 'Foto dibatasi maksimal 5 per verifikasi.' : '';
      });
    } catch (e) {
      if (mounted) setState(() => message = 'Gagal memilih foto: $e');
    }
  }

  Future<void> uploadPhoto(XFile file) async {
    final mime = lookupMimeType(file.name) ?? 'image/jpeg';
    final request = http.MultipartRequest('POST', Uri.parse('$serverUrl/dps/surveys/${widget.surveyId}/verification-media'))
      ..headers['Authorization'] = 'Bearer ${widget.authToken}'
      ..files.add(await http.MultipartFile.fromPath('file', file.path, filename: file.name, contentType: MediaType.parse(mime)));
    final response = await request.send().timeout(const Duration(minutes: 2));
    final body = await response.stream.bytesToString();
    if (response.statusCode < 200 || response.statusCode >= 300) throw Exception('Foto verifikasi gagal diunggah (${response.statusCode}): $body');
  }

  Future<void> submit() async {
    if (survey == null) return;
    if (decision == null) { setState(() => message = 'Pilih keputusan verifikasi terlebih dahulu.'); return; }
    if (position == null) { setState(() => message = 'Ambil GPS lokasi pemeriksaan sebelum mengirim.'); return; }
    if (val('business_condition').isEmpty) { setState(() => message = 'Kondisi nyata usaha wajib diisi.'); return; }
    if (val('actual_business_activity').isEmpty) { setState(() => message = 'Aktivitas usaha aktual wajib diisi.'); return; }
    if (dataMatch == null) { setState(() => message = 'Pilih kesesuaian data survei awal.'); return; }
    if (decision == 'Ditolak' && val('rejection_reason').isEmpty) { setState(() => message = 'Alasan penolakan wajib diisi.'); return; }
    final itemPayload = <Map<String, dynamic>>[];
    for (final row in needs) {
      if (row.name.text.trim().isEmpty && row.unitPrice.text.trim().isEmpty) continue;
      final item = row.toJson();
      if (item['item_name'].toString().isEmpty || (item['quantity'] as double) <= 0 || (item['unit_price'] as double) < 0) {
        setState(() => message = 'Lengkapi nama, jumlah, dan harga setiap item kebutuhan.');
        return;
      }
      itemPayload.add(item);
    }
    if (itemPayload.isEmpty) { setState(() => message = 'Tambahkan minimal satu item kebutuhan UMKM yang terperinci.'); return; }
    setState(() { sending = true; message = 'Mengirim bukti foto verifikasi...'; });
    try {
      for (final photo in verificationPhotos) {
        if (uploadedPhotoPaths.contains(photo.path)) continue;
        await uploadPhoto(photo);
        uploadedPhotoPaths.add(photo.path);
      }
      final payload = <String, dynamic>{
        'decision': decision,
        'business_condition': val('business_condition'),
        'actual_business_activity': val('actual_business_activity'),
        'existing_assets': val('existing_assets'),
        'needs': itemPayload,
        'working_capital_details': val('working_capital_details'),
        'verified_turnover': numVal('verified_turnover'),
        'verified_profit': numVal('verified_profit'),
        'verified_worker_count': intVal('verified_worker_count'),
        'data_match': dataMatch ?? '',
        'discrepancy_notes': val('discrepancy_notes'),
        'verification_notes': val('verification_notes'),
        'rejection_reason': val('rejection_reason'),
        'latitude': position!.latitude,
        'longitude': position!.longitude,
        'accuracy': position!.accuracy,
      };
      setState(() => message = 'Menyimpan hasil verifikasi ke server...');
      final response = await http.post(Uri.parse('$serverUrl/dps/surveys/${widget.surveyId}/verification'), headers: headers, body: jsonEncode(payload)).timeout(const Duration(seconds: 45));
      final decoded = jsonDecode(response.body);
      if (response.statusCode < 200 || response.statusCode >= 300) {
        final detail = decoded is Map ? decoded['detail'] : null;
        throw Exception(detail ?? 'Server menolak hasil verifikasi (${response.statusCode}).');
      }
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(const SnackBar(content: Text('Hasil verifikasi berhasil dikirim ke server.')));
      widget.onSubmitted();
      Navigator.of(context).pop(true);
    } catch (e) {
      if (mounted) setState(() => message = 'Pengiriman gagal: $e\nPeriksa koneksi dan status survei sebelum mencoba lagi. Foto yang sudah terkirim tidak perlu dipilih ulang.');
    } finally {
      if (mounted) setState(() => sending = false);
    }
  }

  Widget textField(String key, String label, {int lines = 1, TextInputType? keyboard, String? hint}) => Padding(
    padding: const EdgeInsets.only(bottom: 12),
    child: EmbossInput(child: TextField(controller: c[key], enabled: !sending, maxLines: lines, keyboardType: keyboard ?? (lines > 1 ? TextInputType.multiline : TextInputType.text), decoration: InputDecoration(labelText: label, hintText: hint))),
  );

  Widget originalDataSection() {
    final entries = <(String, String)>[
      ('Nama pemohon', 'applicant_name'), ('NIK', 'applicant_nik'), ('Telepon', 'applicant_phone'),
      ('Alamat', 'applicant_address'), ('Desa/kelurahan', 'village'), ('Nama usaha', 'business_name'),
      ('Jenis usaha', 'business_type'), ('Tahun mulai usaha', 'business_since'), ('Lokasi usaha', 'business_location'),
      ('Status tempat usaha', 'ownership_status'), ('NIB', 'nib'), ('Modal awal (Rp)', 'capital_amount'),
      ('Omzet awal (Rp)', 'turnover_amount'), ('Laba bersih awal (Rp)', 'net_profit_amount'),
      ('Jumlah pekerja awal', 'worker_count'), ('Status usaha', 'business_active'), ('Kendala usaha', 'business_issues'),
      ('Jenis bantuan yang diminta', 'assistance_types'), ('Perkiraan bantuan (Rp)', 'assistance_amount'),
      ('Alasan pengajuan', 'assistance_reason'), ('Catatan survei', 'notes'),
      ('GPS survei', 'latitude'), ('Longitude survei', 'longitude'), ('Akurasi GPS awal (m)', 'accuracy'),
      ('Petugas survei', 'member_id'), ('Status awal', 'verification_status'),
    ];
    return EmbossCard(child: Padding(padding: const EdgeInsets.all(14), child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
      const Text('A. DATA SURVEI AWAL DPSS (HANYA DIBACA)', style: TextStyle(color: maroon, fontWeight: FontWeight.bold)),
      const SizedBox(height: 10),
      ...entries.map((entry) => Padding(padding: const EdgeInsets.only(bottom: 7), child: Row(crossAxisAlignment: CrossAxisAlignment.start, children: [SizedBox(width: 145, child: Text(entry.$1, style: const TextStyle(color: Colors.black54, fontSize: 12))), Expanded(child: Text(surveyText(entry.$2), style: const TextStyle(fontSize: 13, fontWeight: FontWeight.w500)))]))),
      const Divider(),
      const Text('Foto/dokumen survei awal', style: TextStyle(fontWeight: FontWeight.bold)),
      if (originalMedia.isEmpty) const Text('Belum ada foto atau video yang tercatat.'),
      ...originalMedia.map((media) {
        final url = media['signed_url']?.toString() ?? '';
        final name = media['original_filename']?.toString() ?? media['media_category']?.toString() ?? 'Media';
        final contentType = (media['content_type']?.toString() ?? '').toLowerCase();
        final category = (media['media_category']?.toString() ?? '').toLowerCase();
        final lowerName = name.toLowerCase();
        final isVideo = contentType.startsWith('video/') ||
            category.contains('video') ||
            RegExp(r'\.(mp4|mov|m4v|webm|mkv)$').hasMatch(lowerName);
        if (url.isEmpty) {
          return ListTile(dense: true, title: Text(name), subtitle: const Text('Pratinjau tidak tersedia'));
        }
        if (contentType.startsWith('image/')) {
          return Padding(
            padding: const EdgeInsets.only(top: 8),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(name, style: const TextStyle(fontSize: 12)),
                const SizedBox(height: 4),
                Container(
                  padding: const EdgeInsets.all(3),
                  decoration: Emboss.raised(radius: 12, soft: true),
                  child: ClipRRect(
                  borderRadius: BorderRadius.circular(9),
                  child: Image.network(
                    url,
                    height: 150,
                    width: double.infinity,
                    fit: BoxFit.cover,
                    errorBuilder: (_, _, _) => const Text('Foto tidak dapat ditampilkan.'),
                  ),
                  ),
                ),
              ],
            ),
          );
        }
        if (isVideo) {
          return Padding(
            padding: const EdgeInsets.only(top: 10, bottom: 8),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(name, style: const TextStyle(fontSize: 12, fontWeight: FontWeight.w600)),
                const SizedBox(height: 6),
                OriginalMediaVideoPlayer(url: url),
              ],
            ),
          );
        }
        return ListTile(
          dense: true,
          leading: const Icon(Icons.insert_drive_file_outlined),
          title: Text(name),
          subtitle: const Text('Berkas tersedia di server'),
        );
      }),
    ])));
  }

  Widget needsSection() => EmbossCard(child: Padding(padding: const EdgeInsets.all(14), child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
    const Text('RINCIAN KEBUTUHAN UMKM', style: TextStyle(color: maroon, fontWeight: FontWeight.bold)),
    const SizedBox(height: 5),
    const Text('Tambahkan setiap barang atau kebutuhan secara terpisah. Total dihitung dari jumlah × harga satuan.', style: TextStyle(fontSize: 12, color: Colors.black54)),
    const SizedBox(height: 12),
    ...needs.asMap().entries.map((entry) {
      final index = entry.key; final row = entry.value;
      return EmbossCard(margin: const EdgeInsets.only(bottom: 12), padding: const EdgeInsets.all(10), radius: 14, soft: true, child: Column(children: [
        Row(children: [Expanded(child: Text('Item ${index + 1}', style: const TextStyle(fontWeight: FontWeight.bold, color: maroon))), if (needs.length > 1) IconButton(onPressed: sending ? null : () => setState(() { needs.removeAt(index).dispose(); }), icon: const Icon(Icons.delete_outline, color: Colors.red))]),
        EmbossInput(child: TextField(controller: row.name, enabled: !sending, decoration: const InputDecoration(labelText: 'Nama barang/kebutuhan *'))),
        const SizedBox(height: 10), EmbossInput(child: TextField(controller: row.specification, enabled: !sending, maxLines: 2, decoration: const InputDecoration(labelText: 'Spesifikasi/ukuran/jenis'))),
        const SizedBox(height: 10), Row(children: [Expanded(child: EmbossInput(child: TextField(controller: row.quantity, enabled: !sending, keyboardType: TextInputType.number, onChanged: (_) => setState(() {}), decoration: const InputDecoration(labelText: 'Jumlah *')))), const SizedBox(width: 8), Expanded(child: EmbossInput(child: TextField(controller: row.unit, enabled: !sending, decoration: const InputDecoration(labelText: 'Satuan'))))]),
        const SizedBox(height: 10), EmbossInput(child: TextField(controller: row.unitPrice, enabled: !sending, keyboardType: TextInputType.number, onChanged: (_) => setState(() {}), decoration: const InputDecoration(labelText: 'Harga satuan (Rp) *'))),
        const SizedBox(height: 10), EmbossInput(child: TextField(controller: row.notes, enabled: !sending, decoration: const InputDecoration(labelText: 'Catatan item'))),
      ]));
    }),
    Align(alignment: Alignment.centerLeft, child: EmbossButton(enabled: !(sending || needs.length >= 50), child: OutlinedButton.icon(onPressed: sending || needs.length >= 50 ? null : () => setState(() => needs.add(_NeedRow())), icon: const Icon(Icons.add), label: const Text('Tambah item kebutuhan'), style: Emboss.secondaryStyle(vertical: 12)))),
    const SizedBox(height: 8),
    Builder(builder: (_) {
      final total = needs.fold<double>(0, (sum, row) => sum + (double.tryParse(row.quantity.text.replaceAll(',', '.')) ?? 0) * (double.tryParse(row.unitPrice.text.replaceAll('.', '').replaceAll(',', '.')) ?? 0));
      return EmbossCard(pressed: true, radius: 12, margin: EdgeInsets.zero, padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 12), colors: const [Color(0xFFF3E6C8), Color(0xFFFFF9EA)], child: SizedBox(width: double.infinity, child: Text('Total estimasi: Rp ${total.toStringAsFixed(0)}', style: const TextStyle(fontWeight: FontWeight.bold, color: maroon))));
    }),
  ])));

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(title: Text('Verifikasi UMKM #${widget.surveyId}'), actions: [IconButton(tooltip: 'Muat ulang', onPressed: loading || sending ? null : loadDetail, icon: const Icon(Icons.refresh))]),
      body: loading ? const Center(child: CircularProgressIndicator()) : survey == null ? Center(child: Padding(padding: const EdgeInsets.all(20), child: Text(message))) : ListView(padding: const EdgeInsets.all(14), children: [
        EmbossCard(margin: EdgeInsets.zero, padding: const EdgeInsets.all(12), radius: 14, colors: const [Color(0xFFFFFDF4), Color(0xFFFFF0D2)], borderColor: gold, child: const Text('Periksa kembali kondisi nyata di lokasi. Data survei awal di bawah ini hanya untuk dibaca dan tidak akan ditimpa oleh formulir verifikasi.', style: TextStyle(height: 1.4))),
        const SizedBox(height: 10),
        originalDataSection(),
        EmbossCard(child: Padding(padding: const EdgeInsets.all(14), child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
          const Text('B. FORMULIR PEMERIKSAAN DPS', style: TextStyle(color: maroon, fontWeight: FontWeight.bold)),
          const SizedBox(height: 12),
          textField('business_condition', 'Kondisi nyata tempat/usaha saat dikunjungi', lines: 3),
          textField('actual_business_activity', 'Aktivitas usaha yang benar-benar berjalan', lines: 3),
          textField('existing_assets', 'Aset/peralatan usaha yang sudah dimiliki', lines: 3),
          EmbossInput(child: DropdownButtonFormField<String>(dropdownColor: ivory, borderRadius: BorderRadius.circular(14), initialValue: dataMatch, decoration: const InputDecoration(labelText: 'Kesesuaian dengan data survei awal'), items: const [DropdownMenuItem(value: 'Sesuai', child: Text('Sesuai')), DropdownMenuItem(value: 'Sebagian sesuai', child: Text('Sebagian sesuai')), DropdownMenuItem(value: 'Tidak sesuai', child: Text('Tidak sesuai'))], onChanged: sending ? null : (v) => setState(() => dataMatch = v))),
          const SizedBox(height: 10),
          textField('discrepancy_notes', 'Jelaskan perbedaan data jika ada', lines: 3),
          textField('working_capital_details', 'Rincian kebutuhan modal kerja/bahan baku', lines: 3),
          Row(children: [Expanded(child: textField('verified_turnover', 'Omzet aktual/bulan (Rp)', keyboard: TextInputType.number)), const SizedBox(width: 8), Expanded(child: textField('verified_profit', 'Laba aktual/bulan (Rp)', keyboard: TextInputType.number))]),
          textField('verified_worker_count', 'Jumlah pekerja aktual', keyboard: TextInputType.number),
        ]))),
        needsSection(),
        EmbossCard(child: Padding(padding: const EdgeInsets.all(14), child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
          const Text('C. BUKTI DAN KEPUTUSAN VERIFIKASI', style: TextStyle(color: maroon, fontWeight: FontWeight.bold)),
          const SizedBox(height: 10),
          SizedBox(width: double.infinity, child: EmbossButton(enabled: !sending, child: OutlinedButton.icon(style: Emboss.secondaryStyle(vertical: 14), onPressed: sending ? null : getGps, icon: gettingGps ? const SizedBox(width: 18, height: 18, child: CircularProgressIndicator(strokeWidth: 2)) : const Icon(Icons.my_location), label: Text(gettingGps ? 'Mengambil GPS...' : 'AMBIL GPS LOKASI VERIFIKASI')))),
          const SizedBox(height: 10),
          if (position != null) Padding(padding: const EdgeInsets.only(bottom: 8), child: Text('GPS DPS: ${position!.latitude}, ${position!.longitude} • akurasi ${position!.accuracy.toStringAsFixed(1)} m')),
          EmbossButton(enabled: !(sending || verificationPhotos.length >= 5), child: OutlinedButton.icon(onPressed: sending || verificationPhotos.length >= 5 ? null : pickPhotos, icon: const Icon(Icons.add_a_photo_outlined), label: Text('Pilih foto bukti (${verificationPhotos.length}/5)'), style: Emboss.secondaryStyle(vertical: 13))),
          ...verificationPhotos.asMap().entries.map((entry) => EmbossCard(pressed: true, radius: 12, soft: true, margin: const EdgeInsets.only(top: 8), padding: const EdgeInsets.symmetric(horizontal: 10), child: ListTile(dense: true, contentPadding: EdgeInsets.zero, title: Text(entry.value.name, maxLines: 1, overflow: TextOverflow.ellipsis), trailing: IconButton(onPressed: sending ? null : () => setState(() => verificationPhotos.removeAt(entry.key)), icon: const Icon(Icons.delete_outline, color: Colors.red))))),
          const SizedBox(height: 4),
          textField('verification_notes', 'Catatan hasil pemeriksaan', lines: 3),
          EmbossInput(child: DropdownButtonFormField<String>(dropdownColor: ivory, borderRadius: BorderRadius.circular(14), initialValue: decision, decoration: const InputDecoration(labelText: 'Keputusan akhir *'), items: const [DropdownMenuItem(value: 'Sudah diverifikasi', child: Text('Sudah diverifikasi')), DropdownMenuItem(value: 'Ditolak', child: Text('Ditolak'))], onChanged: sending ? null : (v) => setState(() => decision = v))),
          if (decision == 'Ditolak') ...[const SizedBox(height: 10), textField('rejection_reason', 'Alasan penolakan *', lines: 3)],
          const SizedBox(height: 12),
          SizedBox(width: double.infinity, child: EmbossButton(enabled: !sending, primary: true, child: FilledButton.icon(onPressed: sending ? null : submit, icon: sending ? const SizedBox(width: 18, height: 18, child: CircularProgressIndicator(strokeWidth: 2, color: Colors.white)) : const Icon(Icons.cloud_upload_outlined), label: Text(sending ? 'MENGIRIM...' : 'SIMPAN DAN KIRIM HASIL VERIFIKASI'), style: Emboss.primaryStyle(vertical: 15)))),
        ]))),
        if (message.isNotEmpty) Padding(padding: const EdgeInsets.symmetric(vertical: 12), child: Text(message, style: TextStyle(color: message.toLowerCase().contains('gagal') || message.toLowerCase().contains('wajib') ? Colors.red.shade700 : maroon))),
      ]),
    );
  }
}

