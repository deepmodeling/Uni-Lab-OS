/* 独立 Bullet 桥的 C ABI。

函数名、float 数组布局与 Blender 的 RigidBody 接口一致，便于现有 ctypes 绑定
直接改指向本库；本库不引用 Blender 进程内的任何符号。 */

#ifndef UNILAB_BULLET_RB_ABI_H
#define UNILAB_BULLET_RB_ABI_H

#if defined(_WIN32)
#  define LAB_BULLET_API __declspec(dllexport)
#else
#  define LAB_BULLET_API __attribute__((visibility("default")))
#endif

#ifdef __cplusplus
extern "C" {
#endif

typedef struct rbDynamicsWorld rbDynamicsWorld;
typedef struct rbRigidBody rbRigidBody;
typedef struct rbCollisionShape rbCollisionShape;
typedef struct rbMeshData rbMeshData;
typedef struct rbConstraint rbConstraint;

/* 动力学世界 */
LAB_BULLET_API rbDynamicsWorld *RB_dworld_new(const float gravity[3]);
LAB_BULLET_API void RB_dworld_delete(rbDynamicsWorld *world);
LAB_BULLET_API void RB_dworld_get_gravity(rbDynamicsWorld *world, float g_out[3]);
LAB_BULLET_API void RB_dworld_set_gravity(rbDynamicsWorld *world, const float g_in[3]);
LAB_BULLET_API void RB_dworld_set_solver_iterations(rbDynamicsWorld *world, int iterations);
LAB_BULLET_API void RB_dworld_set_split_impulse(rbDynamicsWorld *world, int enabled);
LAB_BULLET_API void RB_dworld_step_simulation(rbDynamicsWorld *world,
                                              float time_step,
                                              int max_sub_steps,
                                              float fixed_time_step);
LAB_BULLET_API void RB_dworld_add_body(rbDynamicsWorld *world, rbRigidBody *body, int col_groups);
LAB_BULLET_API void RB_dworld_remove_body(rbDynamicsWorld *world, rbRigidBody *body);

/* 刚体 */
LAB_BULLET_API rbRigidBody *RB_body_new(rbCollisionShape *shape,
                                        const float loc[3],
                                        const float rot[4]);
LAB_BULLET_API void RB_body_delete(rbRigidBody *body);
LAB_BULLET_API float RB_body_get_mass(rbRigidBody *body);
LAB_BULLET_API void RB_body_set_mass(rbRigidBody *body, float mass);
LAB_BULLET_API float RB_body_get_friction(rbRigidBody *body);
LAB_BULLET_API void RB_body_set_friction(rbRigidBody *body, float friction);
LAB_BULLET_API float RB_body_get_restitution(rbRigidBody *body);
LAB_BULLET_API void RB_body_set_restitution(rbRigidBody *body, float restitution);
LAB_BULLET_API void RB_body_set_damping(rbRigidBody *body, float linear, float angular);
LAB_BULLET_API void RB_body_set_sleep_thresh(rbRigidBody *body, float linear, float angular);
LAB_BULLET_API void RB_body_set_activation_state(rbRigidBody *body, int use_deactivation);
LAB_BULLET_API void RB_body_activate(rbRigidBody *body);
LAB_BULLET_API void RB_body_deactivate(rbRigidBody *body);
LAB_BULLET_API void RB_body_get_linear_velocity(rbRigidBody *body, float v_out[3]);
LAB_BULLET_API void RB_body_set_linear_velocity(rbRigidBody *body, const float v_in[3]);
LAB_BULLET_API void RB_body_get_angular_velocity(rbRigidBody *body, float v_out[3]);
LAB_BULLET_API void RB_body_set_angular_velocity(rbRigidBody *body, const float v_in[3]);
LAB_BULLET_API void RB_body_set_linear_factor(rbRigidBody *body, float x, float y, float z);
LAB_BULLET_API void RB_body_set_angular_factor(rbRigidBody *body, float x, float y, float z);
LAB_BULLET_API void RB_body_set_loc_rot(rbRigidBody *body, const float loc[3], const float rot[4]);
LAB_BULLET_API void RB_body_get_position(rbRigidBody *body, float v_out[3]);
LAB_BULLET_API void RB_body_get_orientation(rbRigidBody *body, float v_out[4]);
LAB_BULLET_API void RB_body_get_transform_matrix(rbRigidBody *body, float m_out[4][4]);
LAB_BULLET_API void RB_body_apply_central_force(rbRigidBody *body, const float v_in[3]);

/* 碰撞形状 */
LAB_BULLET_API rbCollisionShape *RB_shape_new_box(float x, float y, float z);
LAB_BULLET_API rbCollisionShape *RB_shape_new_compound(void);
LAB_BULLET_API void RB_compound_add_child_shape(rbCollisionShape *parent,
                                                rbCollisionShape *child,
                                                const float loc[3],
                                                const float rot[4]);
LAB_BULLET_API rbCollisionShape *RB_shape_new_convex_hull(
    const float *vertices, int stride, int count, float margin, bool *can_embed);
LAB_BULLET_API rbMeshData *RB_trimesh_data_new(int num_tris, int num_verts);
LAB_BULLET_API void RB_trimesh_add_vertices(rbMeshData *mesh,
                                            float *vertices,
                                            int num_verts,
                                            int vertex_stride);
LAB_BULLET_API void RB_trimesh_add_triangle_indices(
    rbMeshData *mesh, int num, int index0, int index1, int index2);
LAB_BULLET_API void RB_trimesh_finish(rbMeshData *mesh);
LAB_BULLET_API rbCollisionShape *RB_shape_new_trimesh(rbMeshData *mesh);
LAB_BULLET_API rbCollisionShape *RB_shape_new_gimpact_mesh(rbMeshData *mesh);
LAB_BULLET_API void RB_shape_delete(rbCollisionShape *shape);
LAB_BULLET_API float RB_shape_get_margin(rbCollisionShape *shape);
LAB_BULLET_API void RB_shape_set_margin(rbCollisionShape *shape, float margin);

/* 约束 */
LAB_BULLET_API void RB_dworld_add_constraint(rbDynamicsWorld *world,
                                             rbConstraint *constraint,
                                             int disable_collisions);
LAB_BULLET_API void RB_dworld_remove_constraint(rbDynamicsWorld *world,
                                                rbConstraint *constraint);
LAB_BULLET_API rbConstraint *RB_constraint_new_point(const float pivot[3],
                                                     rbRigidBody *body1,
                                                     rbRigidBody *body2);
LAB_BULLET_API rbConstraint *RB_constraint_new_fixed(float pivot[3],
                                                     float orn[4],
                                                     rbRigidBody *body1,
                                                     rbRigidBody *body2);
LAB_BULLET_API rbConstraint *RB_constraint_new_hinge(float pivot[3],
                                                     float orn[4],
                                                     rbRigidBody *body1,
                                                     rbRigidBody *body2);
LAB_BULLET_API rbConstraint *RB_constraint_new_slider(float pivot[3],
                                                      float orn[4],
                                                      rbRigidBody *body1,
                                                      rbRigidBody *body2);
LAB_BULLET_API rbConstraint *RB_constraint_new_piston(float pivot[3],
                                                      float orn[4],
                                                      rbRigidBody *body1,
                                                      rbRigidBody *body2);
LAB_BULLET_API rbConstraint *RB_constraint_new_6dof(float pivot[3],
                                                    float orn[4],
                                                    rbRigidBody *body1,
                                                    rbRigidBody *body2);
LAB_BULLET_API rbConstraint *RB_constraint_new_6dof_spring(float pivot[3],
                                                           float orn[4],
                                                           rbRigidBody *body1,
                                                           rbRigidBody *body2);
LAB_BULLET_API rbConstraint *RB_constraint_new_6dof_spring2(float pivot[3],
                                                            float orn[4],
                                                            rbRigidBody *body1,
                                                            rbRigidBody *body2);
LAB_BULLET_API rbConstraint *RB_constraint_new_motor(float pivot[3],
                                                     float orn[4],
                                                     rbRigidBody *body1,
                                                     rbRigidBody *body2);
LAB_BULLET_API void RB_constraint_delete(rbConstraint *constraint);
LAB_BULLET_API void RB_constraint_set_enabled(rbConstraint *constraint, int enabled);
LAB_BULLET_API void RB_constraint_set_limits_hinge(rbConstraint *constraint,
                                                   float lower,
                                                   float upper);
LAB_BULLET_API void RB_constraint_set_limits_slider(rbConstraint *constraint,
                                                    float lower,
                                                    float upper);
LAB_BULLET_API void RB_constraint_set_limits_piston(rbConstraint *constraint,
                                                    float lin_lower,
                                                    float lin_upper,
                                                    float ang_lower,
                                                    float ang_upper);
LAB_BULLET_API void RB_constraint_set_limits_6dof(rbConstraint *constraint,
                                                  int axis,
                                                  float lower,
                                                  float upper);
LAB_BULLET_API void RB_constraint_set_enable_motor(rbConstraint *constraint,
                                                   int enable_lin,
                                                   int enable_ang);
LAB_BULLET_API void RB_constraint_set_max_impulse_motor(rbConstraint *constraint,
                                                        float max_impulse_lin,
                                                        float max_impulse_ang);
LAB_BULLET_API void RB_constraint_set_target_velocity_motor(rbConstraint *constraint,
                                                            float velocity_lin,
                                                            float velocity_ang);
LAB_BULLET_API void RB_constraint_set_solver_iterations(rbConstraint *constraint, int iterations);
LAB_BULLET_API void RB_constraint_set_breaking_threshold(rbConstraint *constraint,
                                                         float threshold);

/* Blender 侧的 lab_* 扩展（接触流、射线、惯量覆盖等）。
 * rbContact 的字段顺序必须与 Python ctypes Contact 一致。 */
typedef struct rbContact {
  void *a;
  void *b;
  double position[3];
  double normal[3];
  double separation;
  double impulse;
} rbContact;

LAB_BULLET_API int lab_scalar_bytes(void);
LAB_BULLET_API int lab_abi_version(void);
LAB_BULLET_API int lab_contacts(rbDynamicsWorld *world, rbContact *out, int capacity);
LAB_BULLET_API void lab_friction_2d(rbDynamicsWorld *world);
LAB_BULLET_API void lab_inertia(rbRigidBody *body,
                                double mass,
                                double x,
                                double y,
                                double z);
LAB_BULLET_API void lab_ignore(rbRigidBody *body_a, rbRigidBody *body_b, int ignore);
LAB_BULLET_API void lab_force_at(rbRigidBody *body, const double *force, const double *point);
LAB_BULLET_API void lab_shape_aabb(rbCollisionShape *shape, double min[3], double max[3]);
LAB_BULLET_API void *lab_ray(rbDynamicsWorld *world,
                             const double *from,
                             const double *to,
                             void *exclude,
                             double *distance);

#ifdef __cplusplus
}
#endif

#endif /* UNILAB_BULLET_RB_ABI_H */
